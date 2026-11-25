"""Phase E : ordres limite, choix d'exécution, ordres en attente, arbitrage simulé."""
import pytest

from tests.test_agents import BUY, FakeExchange
from trading_agent.agents import ArbitrageAnalyst, Coordinator, ExecutionIntelligence
from trading_agent.config import Settings
from trading_agent.events import Event, EventBus
from trading_agent.models import PaperOrder, PaperPortfolio, Side, Signal
from trading_agent.paper import PaperBroker
from trading_agent.risk import RiskManager

H = 3_600_000


def order(qty=1.0):
    return PaperOrder("BTC/EUR", Side.BUY, qty, 100.0, 95.0)


def candle(i, low, open_=100.0, volume=1000.0):
    return [i * H, open_, 101.0, low, 100.0, volume]


def test_limit_order_fills_only_when_price_trades_through():
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    placed = broker.place_limit(order(), 99.0, placed_ms=0, expires_ms=3 * H, maker_fee=0.0015)
    assert placed["status"] == "placed" and broker.reserved() == pytest.approx(99.0 * 1.0015)
    assert broker.check_pending("BTC/EUR", [candle(1, 99.0)]) == []        # simple contact : rien
    filled = broker.check_pending("BTC/EUR", [candle(1, 99.0), candle(2, 98.5)])[0]
    assert filled["status"] == "filled" and filled["fill_price"] == 99.0
    assert broker.portfolio.positions["BTC/EUR"]["entry_fee"] == pytest.approx(99.0 * 0.0015)
    assert broker.reserved() == 0 and broker.portfolio.cash == pytest.approx(1000 - 99.0 * 1.0015)


def test_limit_order_expires_and_releases_cash():
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    broker.place_limit(order(), 99.0, placed_ms=0, expires_ms=2 * H, maker_fee=0.0015)
    result = broker.check_pending("BTC/EUR", [candle(1, 99.5), candle(2, 99.5)])[0]
    assert result["status"] == "cancelled" and result["reason"] == "expired"
    assert broker.reserved() == 0 and not broker.portfolio.positions and broker.portfolio.cash == 1000.0


def test_reserved_cash_and_pending_orders_are_counted_by_risk():
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    broker.place_limit(order(6.0), 99.0, placed_ms=0, expires_ms=H, maker_fee=0.0015)  # ~595 € réservés
    risk = RiskManager(Settings(_env_file=None))
    from datetime import datetime, timezone
    same = Signal("BTC/EUR", Side.BUY, 0.8, 100.0, 95.0, "t", datetime.now(timezone.utc))
    assert risk.evaluate(same, broker.portfolio).reason == "Pending order already open for this symbol."
    other = Signal("ETH/EUR", Side.BUY, 0.8, 100.0, 95.0, "t", datetime.now(timezone.utc))
    free = PaperPortfolio(cash=1000.0, equity=1000.0, positions={})
    with_pending = risk.evaluate(other, broker.portfolio)
    without = risk.evaluate(other, free)
    assert with_pending.order.quantity < without.order.quantity    # le montant réservé n'est pas dépensable
    rejected = broker.buy(PaperOrder("ETH/EUR", Side.BUY, 9.0, 100.0, 95.0))
    assert rejected["reason"] == "Insufficient simulated cash."


def test_emergency_halt_cancels_pending_orders(tmp_path):
    bus = EventBus(str(tmp_path / "events.jsonl"))
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    coordinator = Coordinator(bus, Settings(_env_file=None), broker)
    broker.place_limit(order(), 99.0, placed_ms=0, expires_ms=H, maker_fee=0.0015)
    coordinator.halt("test")
    assert broker.portfolio.pending == {}
    assert any(e.type == "execution.cancelled" for e in bus.events)


def _book(bid, ask, size=100.0):
    return {"bids": [[bid, size]], "asks": [[ask, size]]}


def test_router_prefers_market_when_saving_is_small_and_limit_when_book_is_thin(tmp_path):
    router = ExecutionIntelligence(EventBus(str(tmp_path / "e.jsonl")), Settings(_env_file=None))
    tight = router.plan(order(), _book(99.99, 100.01), taker=0.0025, maker=0.0015, corr="c")
    assert tight["choice"] == "market"                 # 10 pb d'économie < 15 pb exigés
    wide = router.plan(order(), _book(99.6, 100.0), taker=0.0025, maker=0.0015, corr="c")
    assert wide["choice"] == "limit" and wide["limit_price"] == 99.6
    thin = router.plan(order(5.0), _book(99.99, 100.01, size=1.0), taker=0.0025, maker=0.0025, corr="c")
    assert thin["choice"] == "limit" and thin["options"]["market"]["fill_ratio"] < 1


@pytest.mark.asyncio
async def test_limit_path_end_to_end_then_fill_on_later_candle(tmp_path):
    class FeeExchange(FakeExchange):
        def fees(self, symbol):
            return {"taker": 0.0025, "maker": 0.0015}
    bus = EventBus(str(tmp_path / "events.jsonl"))
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    coordinator = Coordinator(bus, Settings(_env_file=None, regime_filter=False), broker)
    corr = await coordinator.run_cycle(FeeExchange(BUY), "fake", "BTC/EUR", "1m")
    types = [e.type for e in bus.thread(corr)]
    assert "routing.plan" in types and "execution.order_placed" in types
    assert "execution.fill" not in types and len(broker.portfolio.pending) == 1
    pid = next(e for e in bus.thread(corr) if e.type == "trade.proposal").payload["proposal_id"]
    assert not bus.is_approved(pid)                    # une proposition ne sert qu'une fois
    # Bougie suivante : le prix passe sous la limite.
    limit = next(iter(broker.portfolio.pending.values()))["limit_price"]
    corr2 = await coordinator.run_cycle(FeeExchange(BUY + [limit - 2]), "fake", "BTC/EUR", "1m")
    fill = next(e for e in bus.thread(corr2) if e.type == "execution.fill")
    assert fill.payload["order_type"] == "limit" and fill.payload["proposal_id"] == pid
    assert "BTC/EUR" in broker.portfolio.positions


def _side(id_, bid, ask, positions=None, cash=1000.0, taker=0.0025):
    return {"id": id_, "book": _book(bid, ask, 10.0), "positions": positions or {}, "cash": cash,
            "taker": taker}


def test_arbitrage_measures_net_gap_and_never_counts_a_transfer():
    small = ArbitrageAnalyst.measure("BTC/EUR", _side("a", 99.9, 100.0), _side("b", 100.1, 100.2), 500)
    assert small["gross_bps"] == pytest.approx(10.0) and small["net_bps"] == pytest.approx(-40.0)
    assert not small["exploitable"]
    big = ArbitrageAnalyst.measure("BTC/EUR", _side("a", 99.9, 100.0), _side("b", 101.0, 101.1), 500)
    assert big["net_bps"] > 0 and not big["exploitable"]
    assert any("transfert" in r for r in big["reasons"])        # rien à vendre sur b sans transfert
    held = {"BTC/EUR": {"quantity": 10.0}}
    ok = ArbitrageAnalyst.measure("BTC/EUR", _side("a", 99.9, 100.0), _side("b", 101.0, 101.1, held), 500)
    assert ok["exploitable"] and ok["hypothetical_pnl"] == pytest.approx(500 * ok["net_bps"] / 10_000)


@pytest.mark.asyncio
async def test_arbitrage_scan_compares_common_pairs_only(tmp_path):
    settings = Settings(_env_file=None, arbitrage_log_path=str(tmp_path / "arb.jsonl"))
    bus = EventBus(str(tmp_path / "events.jsonl"))
    analyst = ArbitrageAnalyst(bus, settings)

    class Venue:
        def __init__(self, shift):
            self.shift = shift

        async def order_book(self, symbol, limit=50):
            return _book(100.0 + self.shift, 100.1 + self.shift, 50.0)
    venues = []
    for id_, shift, watch in (("a", 0.0, ["BTC/EUR", "ETH/EUR"]), ("b", 0.05, ["BTC/EUR", "SOL/EUR"])):
        broker = PaperBroker(1000.0, 0.0025, 10, None, None)
        venues.append({"id": id_, "ex": Venue(shift), "watchlist": watch, "broker": broker,
                       "coordinator": Coordinator(bus, settings, broker)})
    rows = await analyst.scan(venues, "c")
    assert [r["symbol"] for r in rows] == ["BTC/EUR"]
    assert bus.events[-1].type == "arbitrage.scan" and (tmp_path / "arb.jsonl").exists()
    assert not any(isinstance(e, Event) and e.type.startswith("execution.") for e in bus.events)
