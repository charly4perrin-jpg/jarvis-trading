"""Âge du carnet d'ordres : un ACHAT est refusé si le carnet lu est trop vieux ou d'âge
inconnu ; les sorties ne sont jamais bloquées ; l'absence du champ ne change rien ailleurs."""
from datetime import datetime, timezone

import pytest

from trading_agent.config import Settings
from trading_agent.models import PaperPortfolio, Side, Signal
from trading_agent.risk import RiskManager

MARKET = {"data_age_s": 1.0, "max_age_s": 60.0, "spread_bps": 5.0, "min_amount": 0.0001, "min_cost": 5.0}


def decide(book_age):
    risk = RiskManager(Settings(_env_file=None))
    signal = Signal("BTC/EUR", Side.BUY, 0.8, 100.0, 95.0, "t", datetime.now(timezone.utc))
    market = dict(MARKET) if book_age == "absent" else {**MARKET, "book_age_s": book_age}
    return risk.evaluate(signal, PaperPortfolio(cash=3000.0, equity=3000.0, positions={}), market)


@pytest.mark.parametrize("age", [0.0, 2.5, 30.0, -4.0])
def test_fresh_book_is_accepted(age):
    assert decide(age).approved


@pytest.mark.parametrize("age", [30.1, 3600.0, None, -60.0, float("nan")])
def test_stale_unknown_or_impossible_book_is_refused(age):
    d = decide(age)
    assert not d.approved


def test_reason_is_explicit():
    assert decide(3600.0).reason == "Order book is stale or missing."


def test_without_book_field_behaviour_is_unchanged():
    assert decide("absent").approved                 # backtest, laboratoire : pas de carnet lu


@pytest.mark.asyncio
async def test_live_cycle_refuses_a_buy_on_a_stale_book_and_buys_on_a_fresh_one(tmp_path):
    from trading_agent.agents import Coordinator
    from trading_agent.events import EventBus
    from trading_agent.paper import PaperBroker
    from trading_agent.scenarios import BUY, MIN, SimExchange
    s = Settings(_env_file=None, regime_filter=False, limit_min_saving_bps=1000,
                 learning_dir=str(tmp_path / "l"))
    for stamp, bought in ((len(BUY) * MIN - 3_600_000, False), (None, True)):
        bus = EventBus(str(tmp_path / f"e{bought}.jsonl"))
        broker = PaperBroker(3000.0, 0.0025, 10, None, None)
        await Coordinator(bus, s, broker).run_cycle(SimExchange(BUY, book_ts_ms=stamp), "sim", "BTC/EUR", "1m")
        assert ("BTC/EUR" in broker.portfolio.positions) is bought
