"""Quatre protections reprises de PumpGrok (réécrites pour Jarvis)."""
import json
import math
from datetime import datetime, timedelta, timezone

import ccxt.async_support as ccxt
import pytest

from tests.test_server import make_client, wait_for
from trading_agent.config import Settings
from trading_agent.learning import MAX_TRIALS, ParamStore, propose
from trading_agent.models import Side, Signal
from trading_agent.paper import PaperBroker
from trading_agent.resilience import ExchangeHealth
from trading_agent.risk import RiskManager


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


@pytest.mark.asyncio
async def test_circuit_breaker_survives_a_restart(tmp_path):
    path = str(tmp_path / "breakers" / "bitvavo.json")
    clock = Clock()
    h = ExchangeHealth("bitvavo", retries=0, failure_threshold=2, cooldown_s=300, clock=clock, sleep=clock.sleep)
    h.persist_to(path)

    async def down():
        raise ccxt.NetworkError("coupure")
    for _ in range(2):
        with pytest.raises(ccxt.NetworkError):
            await h.call("ohlcv", down)
    assert h.state == "open"
    restarted = ExchangeHealth("bitvavo", failure_threshold=2, cooldown_s=300)   # nouveau processus
    restarted.persist_to(path)
    assert restarted.state == "open" and restarted.consecutive_failures == 2

    async def ok():
        return 1
    restarted.open_until = 0                     # fin du repos : un essai réussit
    assert await restarted.call("ohlcv", ok) == 1
    saved = json.loads((tmp_path / "breakers" / "bitvavo.json").read_text())
    assert saved["consecutive_failures"] == 0 and saved["open_until_epoch"] == 0


def _signal():
    return Signal("BTC/EUR", Side.BUY, 0.8, 100.0, 95.0, "t", datetime.now(timezone.utc))


def test_fast_loss_across_midnight_is_caught_by_the_rolling_window():
    risk = RiskManager(Settings(_env_file=None))
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    day1 = datetime(2026, 11, 17, 23, 0, tzinfo=timezone.utc)
    broker.mark({}, now=day1)
    broker.portfolio.cash = 984.0                                    # −1,6 % juste après minuit
    broker.mark({}, now=day1 + timedelta(hours=2))
    assert broker.portfolio.daily_pnl == 0                           # la limite du jour a été remise à zéro…
    decision = risk.evaluate(_signal(), broker.portfolio)
    assert decision.reason == "Loss velocity limit reached."         # …mais pas la fenêtre glissante
    broker.mark({}, now=day1 + timedelta(hours=25))                  # le plus haut sort de la fenêtre
    assert risk.evaluate(_signal(), broker.portfolio).approved


def test_small_dip_does_not_trigger_and_marks_stay_bounded():
    risk = RiskManager(Settings(_env_file=None))
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    t0 = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
    for h in range(0, 72, 4):
        broker.mark({}, now=t0 + timedelta(hours=h))
    broker.portfolio.cash = 990.0                                    # −1 % : sous le seuil
    broker.mark({}, now=t0 + timedelta(hours=72))
    assert risk.evaluate(_signal(), broker.portfolio).approved
    first = datetime.fromisoformat(broker.portfolio.equity_marks[0][0])
    assert t0 + timedelta(hours=72) - first <= timedelta(hours=48)


def test_emergency_halt_can_close_every_position(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/cycle")
        events = wait_for(client, "execution.fill")
        assert client.get("/api/state").json()["portfolio"]["positions"]
        reply = client.post("/api/halt", json={"reason": "test", "flatten": True}).json()
        assert reply == {"halted": True, "closed": 1}
        assert client.get("/api/state").json()["portfolio"]["positions"] == {}
        events = client.get("/api/events").json()
        sale = [e for e in events if e["type"] == "execution.fill" and e["payload"].get("reason") == "emergency_flatten"]
        assert len(sale) == 1 and "Fermeture d'urgence" in sale[0]["result"]


def test_emergency_halt_without_flatten_keeps_positions(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/cycle")
        wait_for(client, "execution.fill")
        assert client.post("/api/halt", json={"reason": "test"}).json()["closed"] == 0
        assert client.get("/api/state").json()["portfolio"]["positions"]


def test_trial_budget_blocks_a_setting_found_after_too_many_tries(tmp_path):
    candles = [[i * 3_600_000, p, p * 1.01, p * 0.99, p, 10.0]
               for i, p in enumerate(100 * math.exp(0.002 * i) * (1 + 0.03 * math.sin(i / 7)) for i in range(600))]
    histories = {"BTC/EUR": {"candles": candles, "limits": {}, "fee": 0.0025}}
    settings = Settings(_env_file=None, regime_filter=False)
    fresh = propose("breakout", histories, settings, 3600, previous_trials=0)
    late = propose("breakout", histories, settings, 3600, previous_trials=MAX_TRIALS - 2)
    assert fresh["cumulative_trials"] == fresh["tried"] <= MAX_TRIALS
    assert late["cumulative_trials"] > MAX_TRIALS and not late["accepted"]
    assert any("budget d'essais" in r for r in late["reasons"])
    store = ParamStore(str(tmp_path / "learning"))
    store.save({**fresh, "id": "a"})
    store.save({**late, "id": "b"})
    assert store.trials("breakout") == fresh["tried"] + late["tried"]


def _halted_with_position(tmp_path, stop=120.0):
    from trading_agent.agents import Coordinator
    from trading_agent.events import EventBus
    bus = EventBus(str(tmp_path / "events.jsonl"), str(tmp_path / "HALT"))
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    broker.portfolio.positions["BTC/EUR"] = {"quantity": 1.0, "entry_price": 130.0, "mark_price": 130.0,
                                             "stop_price": stop, "entry_fee": 0.3, "checked_ms": 0,
                                             "strategy": "momentum_demo"}
    broker.portfolio.cash -= 130.3
    broker.mark({})
    coordinator = Coordinator(bus, Settings(_env_file=None, regime_filter=False), broker)
    coordinator.halt("test")
    return bus, broker, coordinator


@pytest.mark.asyncio
async def test_stops_are_still_watched_during_emergency_halt(tmp_path):
    from tests.test_agents import BUY, FakeExchange
    bus, broker, coordinator = _halted_with_position(tmp_path)
    corr = await coordinator.watch_stops(FakeExchange(BUY[:-1] + [110.0]), "fake", "1m")
    types = [e.type for e in bus.thread(corr)]
    sale = next(e for e in bus.thread(corr) if e.type == "execution.fill")
    assert sale.payload["reason"] == "stop" and sale.payload["side"] == "sell"
    assert not broker.portfolio.positions and bus.halted
    assert not any(t in types for t in ("strategy.signal", "trade.proposal", "risk.decision"))


@pytest.mark.asyncio
async def test_stop_watch_keeps_positions_when_no_stop_is_hit(tmp_path):
    from tests.test_agents import FakeExchange
    bus, broker, coordinator = _halted_with_position(tmp_path, stop=50.0)
    corr = await coordinator.watch_stops(FakeExchange([100.0] * 30 + [130.0]), "fake", "1m")
    end = next(e for e in bus.thread(corr) if e.type == "cycle.end")
    assert "aucun stop touché" in end.result and "BTC/EUR" in broker.portfolio.positions
    broker.portfolio.positions.clear()
    assert await coordinator.watch_stops(FakeExchange([100.0] * 5), "fake", "1m") is None
