"""Phase C : appels résilients, surveillance des plateformes, corrélations, multi-plateformes."""
import asyncio
import math
import time
from datetime import datetime, timezone

import ccxt.async_support as ccxt
import pytest
from fastapi.testclient import TestClient

from tests.test_agents import BUY, FakeExchange
from tests.test_server import FakeServerExchange, wait_for
from trading_agent.agents import Coordinator, PortfolioOptimizer, correlation
from trading_agent.config import Settings
from trading_agent.events import EventBus
from trading_agent.models import PaperPortfolio, Side, Signal
from trading_agent.paper import PaperBroker
from trading_agent.resilience import CircuitOpen, ExchangeHealth
from trading_agent.risk import RiskManager
from trading_agent.server import create_app


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def __call__(self):
        return self.t

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def health(clock, **kw):
    return ExchangeHealth("test", clock=clock, sleep=clock.sleep, **kw)


@pytest.mark.asyncio
async def test_network_errors_are_retried_with_growing_delays():
    clock, calls = Clock(), []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ccxt.NetworkError("coupure")
        return "ok"
    h = health(clock, failure_threshold=5)
    assert await h.call("ohlcv", flaky) == "ok"
    assert clock.slept == [1.0, 2.0] and h.consecutive_failures == 0 and h.state == "closed"


@pytest.mark.asyncio
async def test_circuit_opens_fails_fast_then_recovers_after_cooldown():
    clock = Clock()

    async def down():
        raise ccxt.RequestTimeout("pas de réponse")
    h = health(clock, retries=0, failure_threshold=3, cooldown_s=300)
    for _ in range(3):
        with pytest.raises(ccxt.RequestTimeout):
            await h.call("ticker", down)
    assert h.state == "open"
    with pytest.raises(CircuitOpen):
        await h.call("ticker", down)          # plus aucun appel pendant le repos
    clock.t += 301
    assert h.state == "half_open"

    async def back():
        return 1
    assert await h.call("ticker", back) == 1 and h.state == "closed"


@pytest.mark.asyncio
async def test_hanging_call_is_cut_by_timeout_and_exchange_errors_are_not_retried():
    h = ExchangeHealth("test", timeout_s=0.05, retries=0)

    async def hang():
        await asyncio.sleep(5)
    with pytest.raises(asyncio.TimeoutError):
        await h.call("ohlcv", hang)
    calls = []

    async def bad_symbol():
        calls.append(1)
        raise ccxt.BadSymbol("paire inconnue")
    with pytest.raises(ccxt.BadSymbol):
        await h.call("ohlcv", bad_symbol)
    assert len(calls) == 1 and h.consecutive_failures == 1


class BrokenExchange(FakeExchange):
    def __init__(self, closes):
        super().__init__(closes)
        self.health = ExchangeHealth("cassée", retries=0, failure_threshold=1, cooldown_s=600)
        self.health.open_until = self.health.clock() + 600
        self.health.consecutive_failures = 3
        self.health.last_error = "ohlcv : RequestTimeout"


def make(tmp_path, name="a", **kw):
    bus = EventBus(str(tmp_path / f"{name}.jsonl"))
    broker = PaperBroker(1000.0, 0.0025, 10, None, str(tmp_path / f"{name}.json"))
    return bus, broker, Coordinator(bus, Settings(_env_file=None, regime_filter=False, **kw), broker)


@pytest.mark.asyncio
async def test_suspended_exchange_skips_cycle_without_counting_errors(tmp_path):
    bus, broker, coordinator = make(tmp_path)
    corr = await coordinator.run_cycle(BrokenExchange(BUY), "cassée", "BTC/EUR", "1m")
    events = bus.thread(corr)
    status = next(e for e in events if e.type == "exchange.status")
    assert status.payload["tradable"] is False and status.payload["status"] == "down"
    assert "market.snapshot" not in [e.type for e in events]
    assert events[-2].type == "cycle.end" and events[-2].status == "rejected"
    assert coordinator.security.error_cycles == 0 and not bus.halted


@pytest.mark.asyncio
async def test_inconsistent_balance_raises_critical_anomaly_and_halts(tmp_path):
    bus, broker, coordinator = make(tmp_path)
    broker.portfolio.cash = -50.0
    await coordinator.run_cycle(FakeExchange(BUY), "fake", "BTC/EUR", "1m")
    assert bus.halted and "incohérent" in bus.halt_reason


def series(returns, start=100.0):
    out, price = [], start
    for i, r in enumerate(returns):
        price *= math.exp(r)
        out.append((i * 3_600_000, price))
    return out


def test_correlation_of_return_series():
    wave = [0.01 * math.sin(i) + 0.003 * math.cos(3 * i) for i in range(120)]
    assert correlation(series(wave), series(wave)) == pytest.approx(1.0)
    assert correlation(series(wave), series([-r for r in wave])) == pytest.approx(-1.0)
    assert correlation(series(wave[:20]), series(wave[:20])) is None


def _signal(symbol="BTC/EUR"):
    return Signal(symbol, Side.BUY, 0.8, 100.0, 95.0, "test", datetime.now(timezone.utc))


def _portfolio_with(symbol, quantity=10.0, mark=100.0, stop=95.0):
    p = PaperPortfolio(cash=2000.0, equity=3000.0, positions={})
    p.positions[symbol] = {"quantity": quantity, "entry_price": mark, "mark_price": mark,
                           "stop_price": stop, "entry_fee": 0, "checked_ms": 0, "strategy": "x"}
    return p


def test_correlated_positions_share_one_risk_budget(tmp_path):
    settings = Settings(_env_file=None)
    bus = EventBus(str(tmp_path / "e.jsonl"))
    optimizer = PortfolioOptimizer(bus, settings)
    wave = [0.01 * math.sin(i) for i in range(120)]
    closes = {"BTC/EUR": series(wave), "ETH/EUR": series(wave),
              "XYZ/EUR": series([0.01 * math.sin(i * 2.7 + 1) for i in range(120)])}
    portfolio = _portfolio_with("ETH/EUR", quantity=5.0)          # 25 € de risque ouvert
    linked = optimizer.assess(_signal(), portfolio, closes, "c")
    assert linked["correlated_open_risk"] == pytest.approx(25.0)
    risk = RiskManager(settings)
    market = {"data_age_s": 1, "max_age_s": 60, "spread_bps": 1, "correlated_open_risk": 25.0}
    alone = risk.evaluate(_signal(), portfolio, {**market, "correlated_open_risk": 0.0})
    shared = risk.evaluate(_signal(), portfolio, market)
    assert shared.approved and shared.order.quantity < alone.order.quantity
    assert not risk.evaluate(_signal(), portfolio, {**market, "correlated_open_risk": 30.0}).approved
    # Paire sans historique commun : corrélation inconnue, comptée comme totale.
    unknown = optimizer.assess(_signal(), _portfolio_with("NEW/EUR"), closes, "c")
    assert unknown["correlations"][0]["correlation"] is None and unknown["correlated_open_risk"] > 0
    independent = optimizer.assess(_signal(), _portfolio_with("XYZ/EUR"), closes, "c")
    assert independent["correlated_open_risk"] == 0.0


def test_only_validated_quote_currencies_are_tradable():
    risk = RiskManager(Settings(_env_file=None))
    decision = risk.evaluate(_signal("BTC/USDT"), PaperPortfolio(cash=3000.0, equity=3000.0, positions={}))
    assert not decision.approved and decision.reason == "Quote currency not validated."


def test_two_exchanges_have_separate_portfolios(tmp_path):
    settings = Settings(
        _env_file=None, event_log_path=str(tmp_path / "events.jsonl"),
        halt_flag_path=str(tmp_path / "HALT"), paper_ledger_path=str(tmp_path / "ledger.jsonl"),
        paper_state_path=str(tmp_path / "state.json"), lab_dir=str(tmp_path / "lab"),
        notifications_path=str(tmp_path / "n.jsonl"), notify_macos=False, regime_filter=False,
        registry_path=str(tmp_path / "r.json"), evaluations_dir=str(tmp_path / "ev"),
        database_path=str(tmp_path / "j.db"), extra_exchanges=["kraken"], extra_exchange_cash=500,
        pause_path=str(tmp_path / "paused.json"), performance_dir=str(tmp_path / "perf"),
        limit_min_saving_bps=1000, arbitrage_log_path=str(tmp_path / "arb.jsonl"),
        news_enabled=False)
    app = create_app("fake", "BTC/EUR", "1m", settings=settings,
                     exchange_factory=lambda _id: FakeServerExchange(BUY), auto_run=False)
    with TestClient(app) as client:
        client.post("/api/cycle")
        wait_for(client, "audit.thread_recorded")
        for _ in range(100):
            venues = client.get("/api/state").json()["system"]["venues"]
            if all(v["positions"] for v in venues):
                break
            time.sleep(0.02)
        assert [v["exchange"] for v in venues] == ["fake", "kraken"]
        assert all(v["positions"] == 1 for v in venues)
        assert venues[1]["equity"] < 600                     # capital fictif propre à Kraken
        assert (tmp_path / "kraken" / "paper_state.json").exists()
        cycles = [e for e in client.get("/api/events").json() if e["type"] == "cycle.start"]
        assert {e["payload"]["exchange"] for e in cycles} == {"fake", "kraken"}


def test_coordinators_of_several_exchanges_share_one_security_monitor(tmp_path):
    bus, broker, first = make(tmp_path)
    second = Coordinator(bus, Settings(_env_file=None), PaperBroker(500.0, 0.0026, 10, None, None),
                         security=first.security)
    assert second.security is first.security
    assert sum(1 for cb in bus.subscribers if getattr(cb, "__self__", None) is first.security) == 1


def test_pending_limit_orders_count_in_correlated_risk(tmp_path):
    settings = Settings(_env_file=None)
    optimizer = PortfolioOptimizer(EventBus(str(tmp_path / "e.jsonl")), settings)
    wave = [0.01 * math.sin(i) for i in range(120)]
    closes = {"BTC/EUR": series(wave), "ETH/EUR": series(wave)}
    portfolio = PaperPortfolio(cash=3000.0, equity=3000.0, positions={})
    portfolio.pending = {"o1": {"symbol": "ETH/EUR", "quantity": 4.0, "limit_price": 100.0, "stop_price": 95.0,
                                "reserved": 400.0}}
    result = optimizer.assess(_signal(), portfolio, closes, "c")
    assert result["correlated_open_risk"] == pytest.approx(20.0)
    assert result["correlations"][0]["kind"] == "ordre en attente"
