"""Exécutions partielles des ordres limite : un ordre ne prend qu'une part du volume
échangé sous sa limite, le reste attend ou est annulé. Prudence par défaut."""
import math

import pytest

from trading_agent.agents import Coordinator
from trading_agent.config import Settings
from trading_agent.events import EventBus
from trading_agent.models import PaperOrder, Side
from trading_agent.paper import PaperBroker

H = 3_600_000
FEE = 0.0015


def order(qty=1.0):
    return PaperOrder("BTC/EUR", Side.BUY, qty, 100.0, 95.0)


def candle(i, low, volume, high=101.0, open_=100.0):
    # Volume réparti uniformément de low à high : sous 99, la part (99-low)/(high-low).
    return [i * H, open_, high, low, 100.0, volume]


def placed(qty=1.0, expires=5, participation=0.10, cash=1000.0):
    broker = PaperBroker(cash, 0.0025, 10, None, None)
    r = broker.place_limit(order(qty), 99.0, placed_ms=0, expires_ms=expires * H, maker_fee=FEE,
                           max_participation=participation)
    assert r["status"] == "placed"
    return broker


def test_order_fills_over_several_candles_with_average_price_and_fees():
    broker = placed()
    # Bougie 1 : low 97, high 101 → moitié du volume (8) sous 99 = 4 ; 10 % → 0,4.
    first = broker.check_pending("BTC/EUR", [candle(1, 97.0, 8.0, open_=98.0)])
    assert len(first) == 1 and first[0]["partial"] and first[0]["quantity"] == pytest.approx(0.4)
    assert first[0]["fill_price"] == 98.0 and first[0]["remaining_quantity"] == pytest.approx(0.6)
    pending = next(iter(broker.portfolio.pending.values()))
    assert pending["quantity"] == pytest.approx(0.6)
    assert broker.reserved() == pytest.approx(0.6 * 99.0 * (1 + FEE))
    # Bougie 2 : beaucoup de volume → le reste (0,6) à 99.
    second = broker.check_pending("BTC/EUR", [candle(1, 97.0, 8.0, open_=98.0), candle(2, 98.0, 1000.0)])
    assert len(second) == 1 and not second[0]["partial"] and second[0]["quantity"] == pytest.approx(0.6)
    position = broker.portfolio.positions["BTC/EUR"]
    assert position["quantity"] == pytest.approx(1.0)
    assert position["entry_price"] == pytest.approx(0.4 * 98.0 + 0.6 * 99.0)
    assert position["entry_fee"] == pytest.approx((0.4 * 98.0 + 0.6 * 99.0) * FEE)
    assert broker.portfolio.pending == {} and broker.reserved() == 0
    assert broker.portfolio.cash == pytest.approx(1000 - (0.4 * 98.0 + 0.6 * 99.0) * (1 + FEE))


def test_small_order_in_a_liquid_market_fills_at_once():
    broker = placed(qty=0.01)
    result = broker.check_pending("BTC/EUR", [candle(1, 98.0, 500.0)])[0]
    assert result["status"] == "filled" and not result["partial"] and result["quantity"] == pytest.approx(0.01)


@pytest.mark.parametrize("volume", [0.0, -5.0, None, float("nan"), float("inf"), "abc"])
def test_missing_or_invalid_volume_fills_nothing(volume):
    broker = placed()
    assert broker.check_pending("BTC/EUR", [candle(1, 97.0, volume)]) == []
    assert not broker.portfolio.positions and broker.portfolio.cash == 1000.0
    assert broker.reserved() == pytest.approx(99.0 * (1 + FEE))


def test_candle_without_volume_column_fills_nothing():
    broker = placed()
    assert broker.check_pending("BTC/EUR", [[1 * H, 100.0, 101.0, 97.0, 100.0]]) == []
    assert not broker.portfolio.positions


def test_touching_the_limit_is_still_not_enough():
    broker = placed()
    assert broker.check_pending("BTC/EUR", [candle(1, 99.0, 10_000.0)]) == []


def test_remainder_is_cancelled_at_expiry_and_cash_released():
    broker = placed(expires=2)
    results = broker.check_pending("BTC/EUR", [candle(1, 97.0, 8.0, open_=98.0), candle(2, 99.5, 20.0)])
    assert [r["status"] for r in results] == ["filled", "cancelled"]
    cancelled = results[1]
    assert cancelled["reason"] == "expired" and cancelled["filled_quantity"] == pytest.approx(0.4)
    assert broker.portfolio.pending == {} and broker.reserved() == 0
    assert broker.portfolio.positions["BTC/EUR"]["quantity"] == pytest.approx(0.4)
    assert broker.portfolio.cash == pytest.approx(1000 - 0.4 * 98.0 * (1 + FEE))


def test_remainder_is_cancelled_when_the_stop_sold_the_bought_part():
    broker = placed()
    broker.check_pending("BTC/EUR", [candle(1, 97.0, 8.0, open_=98.0)])
    sold = broker.check_stops("BTC/EUR", [candle(2, 94.0, 20.0, open_=96.0)])
    assert sold["status"] == "filled" and "BTC/EUR" not in broker.portfolio.positions
    # Bougie suivante sous la limite : on ne rachète pas, le reste est annulé.
    results = broker.check_pending("BTC/EUR", [candle(3, 90.0, 10_000.0)])
    assert len(results) == 1 and results[0]["reason"] == "position_closed"
    assert "BTC/EUR" not in broker.portfolio.positions and broker.portfolio.pending == {}


def test_reserve_never_negative_and_cash_conserved_over_many_small_fills():
    broker = placed(qty=2.0, expires=60, participation=0.05)
    candles = [candle(i, 98.0, 7.0, open_=98.5) for i in range(1, 60)]
    broker.check_pending("BTC/EUR", candles)
    position = broker.portfolio.positions["BTC/EUR"]
    left = sum(o["quantity"] for o in broker.portfolio.pending.values())
    assert position["quantity"] + left == pytest.approx(2.0)
    assert broker.reserved() >= 0 and all(math.isfinite(x) for x in (broker.portfolio.cash, broker.reserved()))
    spent = position["quantity"] * position["entry_price"] + position["entry_fee"]
    assert broker.portfolio.cash == pytest.approx(1000 - spent)


def test_invalid_participation_is_refused():
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    for bad in (0.0, -0.1, 1.5, float("nan")):
        r = broker.place_limit(order(), 99.0, 0, H, FEE, max_participation=bad)
        assert r["status"] == "rejected" and broker.portfolio.pending == {}


def test_emergency_halt_cancels_the_remainder_and_keeps_the_bought_part(tmp_path):
    bus = EventBus(str(tmp_path / "events.jsonl"))
    broker = placed()
    coordinator = Coordinator(bus, Settings(_env_file=None), broker)
    broker.check_pending("BTC/EUR", [candle(1, 97.0, 8.0, open_=98.0)])
    coordinator.halt("test")
    assert broker.portfolio.pending == {} and broker.reserved() == 0
    assert broker.portfolio.positions["BTC/EUR"]["quantity"] == pytest.approx(0.4)
    assert any(e.type == "execution.cancelled" for e in bus.events)


def test_execution_agent_explains_partial_fill_and_cancelled_remainder(tmp_path):
    bus = EventBus(str(tmp_path / "events.jsonl"))
    broker = placed(expires=2)
    coordinator = Coordinator(bus, Settings(_env_file=None), broker)
    coordinator.execution.check_pending("BTC/EUR", [candle(1, 97.0, 8.0, open_=98.0), candle(2, 99.5, 20.0)], "c1")
    texts = [e.result for e in bus.events if e.correlation_id == "c1"]
    assert any("exécution partielle" in t and "le reste attend" in t for t in texts)
    assert any("Reste de l'ordre limite" in t and "expiré" in t for t in texts)


def test_old_saved_pending_orders_without_new_fields_still_work():
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    broker.portfolio.pending["old"] = {
        "order_id": "old", "symbol": "BTC/EUR", "quantity": 1.0, "limit_price": 99.0, "stop_price": 95.0,
        "strategy": "momentum_demo", "reserved": 99.0 * (1 + FEE), "maker_fee": FEE, "placed_ms": 0,
        "checked_ms": 0, "expires_ms": 5 * H, "proposal_id": None}
    result = broker.check_pending("BTC/EUR", [candle(1, 98.0, 10_000.0)])[0]
    assert result["status"] == "filled" and result["quantity"] == pytest.approx(1.0)
