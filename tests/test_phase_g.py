"""Phase G : scénarios extrêmes, mode Shadow, Learning Manager, actualités."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_agents import BUY, FakeExchange
from trading_agent import news, scenarios
from trading_agent.agents import Coordinator
from trading_agent.config import Settings
from trading_agent.events import EventBus
from trading_agent.learning import ParamStore, candidates, effective, tunable, validate, with_params
from trading_agent.models import Side, Signal
from trading_agent.paper import PaperBroker
from trading_agent.shadow import ShadowBook
from trading_agent.strategies import get_strategy

H = 3_600_000


@pytest.mark.asyncio
async def test_all_extreme_scenarios_end_safely():
    report = await scenarios.run_all()
    failed = [r for r in report["results"] if not r["passed"]]
    assert not failed, failed
    assert report["total"] == len(scenarios.SCENARIOS) >= 10


def test_tests_never_write_into_real_data_folder():
    assert "isolé" in Settings().event_log_path and "isolé" in Settings().shadow_dir


def _signal(entry=100.0, stop=95.0):
    return Signal("BTC/EUR", Side.BUY, 0.8, entry, stop, "t", datetime.now(timezone.utc), "breakout")


def test_shadow_resolves_blocked_signals_prudently(tmp_path):
    book = ShadowBook(str(tmp_path / "s.json"), fee_rate=0.0025, slippage_bps=10)
    book.record(_signal(), "régime", "tendance baissière", candle_ms=0, corr="a")
    book.record(_signal(), "risque", "limite", candle_ms=0, corr="b")
    both = [[H, 100.0, 111.0, 94.0, 100.0, 1.0]]      # touche l'objectif ET le stop : on retient le stop
    closed = book.update("BTC/EUR", both)
    assert {t["exit_reason"] for t in closed} == {"stop"} and all(t["r"] < -1 for t in closed)
    book.record(_signal(), "régime", "x", candle_ms=H, corr="c")
    win = book.update("BTC/EUR", [[2 * H, 100.0, 111.0, 99.0, 110.0, 1.0]])[0]
    assert win["exit_reason"] == "objectif" and 1.8 < win["r"] < 2.0      # 2 R moins les coûts
    summary = ShadowBook(str(tmp_path / "s.json"), 0.0025, 10).summary()   # relu depuis le disque
    assert summary["by_blocker"]["régime"]["resolved"] == 2 and summary["by_blocker"]["risque"]["wins"] == 0


def test_shadow_reference_is_buy_and_hold(tmp_path):
    book = ShadowBook(str(tmp_path / "s.json"), 0.0025, 10)
    start = book.mark_reference(["BTC/EUR", "ETH/EUR"], {"BTC/EUR": [(0, 100.0)], "ETH/EUR": [(0, 50.0)]}, 1000.0)
    later = book.mark_reference(["BTC/EUR", "ETH/EUR"], {"BTC/EUR": [(1, 110.0)], "ETH/EUR": [(1, 45.0)]}, 990.0)
    assert start["return"] == 0 and later["return"] == pytest.approx(0.0) and later["start_equity"] == 1000.0


@pytest.mark.asyncio
async def test_regime_blocked_signal_is_tracked_in_shadow(tmp_path):
    settings = Settings(_env_file=None, regime_filter=True)
    bus = EventBus(str(tmp_path / "e.jsonl"))
    coordinator = Coordinator(bus, settings, PaperBroker(1000.0, 0.0025, 10, None, None))
    await coordinator.run_cycle(FakeExchange(BUY), "fake", "BTC/EUR", "1m")
    summary = coordinator.shadow("fake").summary()
    assert summary["by_blocker"]["régime"]["signals"] == 1
    assert Path(settings.shadow_dir, "fake.json").exists()


def test_learning_can_only_touch_strategy_parameters_within_bounds():
    strat = get_strategy("breakout")
    params = tunable(strat)
    assert params == {"entry.lookback": 48, "entry.trend": 100, "exit.lookback": 24}
    with pytest.raises(PermissionError):
        validate(strat, {"entry.lookback": 200})                 # plus de 2 fois l'origine
    with pytest.raises(PermissionError):
        validate(strat, {"max_daily_loss_pct": 0.5})             # règle de risque : interdit
    tuned = with_params(strat, {"entry.lookback": 36})
    assert tunable(tuned)["entry.lookback"] == 48 and tuned.entry.keywords == {"lookback": 36}
    assert all(len(c) == 1 for c in candidates(strat)) and len(candidates(strat)) == 6


def test_applying_needs_a_person_and_an_accepted_version_and_can_roll_back(tmp_path):
    store = ParamStore(str(tmp_path / "learning"))
    base = {"strategy": "breakout", "created_at": "2026-11-17T00:00:00+00:00", "tried": 6}
    store.save({**base, "id": "bad", "accepted": False, "reasons": ["perd de l'argent"], "best": None})
    store.save({**base, "id": "good", "accepted": True, "reasons": [],
                "best": {"params": {"entry.lookback": 36}}})
    with pytest.raises(PermissionError):
        store.apply("good", by="")
    with pytest.raises(PermissionError, match="perd"):
        store.apply("bad", by="test")
    store.apply("good", by="test")
    assert effective("breakout", store).entry.keywords == {"lookback": 36}
    store.rollback("breakout", by="test")
    assert store.params("breakout") == {} and effective("breakout", store) is get_strategy("breakout")
    with pytest.raises(ValueError):
        store.rollback("breakout", by="test")


SUMMARY = {"page": {"url": "https://status.example"}, "status": {"indicator": "major", "description": "Partial outage"},
           "incidents": [
               {"name": "Spot trading API degraded", "status": "investigating", "impact": "major"},
               {"name": "Bitcoin (BTC) Funding Delays", "status": "identified", "impact": "minor"},
               {"name": "KAVA deposits paused", "status": "monitoring", "impact": "minor"},
               {"name": "Order matching issue", "status": "resolved", "impact": "critical"}],
           "scheduled_maintenances": [{"name": "Trading engine upgrade", "status": "scheduled", "impact": "maintenance"}]}


def test_news_are_verified_and_only_live_major_trading_incidents_block():
    r = news.classify("kraken", SUMMARY, ["BTC/EUR", "ETH/EUR"])
    rel = {i["title"]: i["relevance"] for i in r["items"]}
    assert r["blocking"] == ["Spot trading API degraded"]
    assert rel["Bitcoin (BTC) Funding Delays"] == "actif suivi" and rel["KAVA deposits paused"] == "autre"
    assert rel["Order matching issue"] == "trading"          # résolu : plus bloquant
    assert all(i["verified"] and i["interpretation"] is None for i in r["items"])


@pytest.mark.asyncio
async def test_blocking_news_suspends_only_that_exchange(tmp_path):
    bus = EventBus(str(tmp_path / "e.jsonl"))
    coordinator = Coordinator(bus, Settings(_env_file=None, regime_filter=False),
                              PaperBroker(1000.0, 0.0025, 10, None, None))
    await coordinator.news.check({"kraken": ["BTC/EUR"], "bitvavo": ["BTC/EUR"]}, "n",
                                 fetcher=lambda url: SUMMARY if "kraken" in url else {"status": {}})
    assert coordinator.news.blocking == {"kraken": ["Spot trading API degraded"]}
    corr = await coordinator.run_cycle(FakeExchange(BUY), "kraken", "BTC/EUR", "1m")
    status = next(e for e in bus.thread(corr) if e.type == "exchange.status")
    assert not status.payload["tradable"] and "incident officiel" in status.result
    corr = await coordinator.run_cycle(FakeExchange(BUY), "bitvavo", "BTC/EUR", "1m")
    assert next(e for e in bus.thread(corr) if e.type == "exchange.status").payload["tradable"]


@pytest.mark.asyncio
async def test_unreachable_status_page_blocks_nothing(tmp_path):
    def down(url):
        raise TimeoutError("pas de réponse")
    result = await news.collect({"kraken": ["BTC/EUR"]}, down)
    assert result["kraken"]["blocking"] == [] and "TimeoutError" in result["kraken"]["error"]


@pytest.mark.asyncio
async def test_daily_lab_plays_on_daily_candles_with_its_own_portfolios(tmp_path):
    from trading_agent.lab import Lab
    D = 86_400_000

    class DailyExchange(FakeExchange):
        async def ohlcv(self, symbol, timeframe="1m", limit=100, since=None):
            assert timeframe == "1d"
            return [[i * D, c, c + 1, c - 1, c, 1.0] for i, c in enumerate(self.closes)]

        def now_ms(self):
            return len(self.closes) * D

    settings = Settings(_env_file=None, regime_filter=False)
    bus = EventBus(str(tmp_path / "e.jsonl"))
    coordinator = Coordinator(bus, settings, PaperBroker(1000.0, 0.0025, 10, None, None))
    lab = Lab(settings, str(tmp_path / "lab_1d"))
    trades = await coordinator.run_daily_lab(lab, DailyExchange(BUY), "fake", ["BTC/EUR"])
    assert any(t["strategy"] == "momentum_demo" and t["side"] == "buy" for t in trades)
    event = next(e for e in bus.events if e.type == "strategy.lab")
    assert event.payload["timeframe"] == "1d" and "journalier" in event.result
    assert not coordinator.broker.portfolio.positions          # le portefeuille principal n'est pas touché
    again = await coordinator.run_daily_lab(lab, DailyExchange(BUY), "fake", ["BTC/EUR"])
    assert again == []                                           # même bougie : pas de second achat
