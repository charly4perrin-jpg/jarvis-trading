"""Réconciliation papier ↔ backtest, lot 1 : rejouer les décisions de l'essai sur les mêmes
bougies ; tout doute donne « non vérifiable », jamais « conforme »."""
import pytest

from tests.test_agents import BUY
from tests.test_server import FakeServerExchange
from trading_agent import reconcile
from trading_agent.agents import Coordinator
from trading_agent.config import Settings
from trading_agent.events import EventBus
from trading_agent.models import PaperOrder, Side
from trading_agent.paper import PaperBroker


def settings(tmp_path):
    return Settings(_env_file=None, regime_filter=False, learning_dir=str(tmp_path / "learning"))


async def recorded_cycles(tmp_path, closes=BUY, rounds=2):
    bus = EventBus(str(tmp_path / "events.jsonl"))
    coordinator = Coordinator(bus, settings(tmp_path), PaperBroker(1000.0, 0.0025, 10, None, None))
    for _ in range(rounds):
        await coordinator.run_cycle(FakeServerExchange(closes), "fake", "BTC/EUR", "1m")
    return list(bus.events)


@pytest.mark.asyncio
async def test_same_data_same_code_gives_conforme(tmp_path):
    events = await recorded_cycles(tmp_path)
    snap = next(e for e in events if e.type == "market.snapshot").payload
    assert snap["candles_sha256"] and snap["first_candle_ms"] is not None
    report = await reconcile.reconcile(events, lambda _id: FakeServerExchange(BUY), settings(tmp_path))
    decided = [r for r in report["rows"] if r["detail"] != "aucune décision dans ce cycle"]
    assert decided and all(r["status"] == "conforme" for r in decided)


@pytest.mark.asyncio
async def test_different_data_is_not_verifiable_never_conforme(tmp_path):
    events = await recorded_cycles(tmp_path)
    other = [c * 1.01 for c in BUY]
    report = await reconcile.reconcile(events, lambda _id: FakeServerExchange(other), settings(tmp_path))
    assert report["rows"] and all(r["status"] == "non vérifiable" for r in report["rows"])


@pytest.mark.asyncio
async def test_tampered_record_shows_the_first_divergent_step(tmp_path):
    events = await recorded_cycles(tmp_path, rounds=1)
    reg = next(e for e in events if e.type == "regime.detected")
    reg.payload["regime"] = {**(reg.payload["regime"] or {}), "trend": "inventé"}
    report = await reconcile.reconcile(events, lambda _id: FakeServerExchange(BUY), settings(tmp_path))
    assert report["rows"][0]["status"] == "divergence" and report["rows"][0]["detail"] == "régime de marché"


@pytest.mark.asyncio
async def test_exchange_failure_is_not_verifiable(tmp_path):
    events = await recorded_cycles(tmp_path, rounds=1)

    class Broken(FakeServerExchange):
        async def ohlcv(self, *a, **k):
            raise ConnectionError("réseau coupé")
    report = await reconcile.reconcile(events, lambda _id: Broken(BUY), settings(tmp_path))
    assert report["rows"][0]["status"] == "non vérifiable" and "réseau coupé" in report["rows"][0]["detail"]


@pytest.mark.asyncio
async def test_live_market_fill_is_replayed_on_the_recorded_book(tmp_path):
    bus = EventBus(str(tmp_path / "events.jsonl"))
    s = Settings(_env_file=None, regime_filter=False, limit_min_saving_bps=1000,
                 learning_dir=str(tmp_path / "learning"))
    coordinator = Coordinator(bus, s, PaperBroker(1000.0, 0.0025, 10, None, None))
    await coordinator.run_cycle(FakeServerExchange(BUY), "fake", "BTC/EUR", "1m")
    events = list(bus.events)
    book = next(e for e in events if e.type == "market.order_book").payload
    assert book["asks"] and book["bids"] and book["read_ms"]       # même lecture, enregistrée
    assert any(e.type == "execution.fill" for e in events)
    report = await reconcile.reconcile(events, lambda _id: FakeServerExchange(BUY), s)
    assert report["rows"][-1]["execution"].startswith("conforme")


MIDNIGHT_37 = 1_795_392_037_000          # une décision à 00:00:37 UTC


def _fill_after_midnight():
    broker = PaperBroker(1000.0, 0.0025, 10, None, None, max_slippage_bps=500)
    order = PaperOrder("BTC/EUR", Side.BUY, 1.0, 100.0, 95.0)    # ouverture de la bougie : 100 €
    fill = broker.buy(order, asks=[[103.0, 5.0]])
    book = {"asks": [[103.0, 5.0]], "bids": [[102.9, 5.0]], "read_ms": MIDNIGHT_37}
    return fill, book


def test_replay_uses_the_recorded_book_not_the_open():
    fill, book = _fill_after_midnight()
    assert fill["fill_price"] == pytest.approx(103.0)
    assert reconcile.replay_execution(book, fill)[0] == "conforme"


def test_missing_book_is_not_verifiable():
    fill, _ = _fill_after_midnight()
    assert reconcile.replay_execution(None, fill)[0] == "non vérifiable"


def test_another_book_shows_a_price_divergence():
    fill, _ = _fill_after_midnight()
    status, detail = reconcile.replay_execution({"asks": [[100.0, 5.0]]}, fill)
    assert status == "divergence" and detail.startswith("prix")


def test_limit_order_and_old_records_are_not_verifiable():
    fill, book = _fill_after_midnight()
    assert reconcile.replay_execution(book, {**fill, "order_type": "limit"})[0] == "non vérifiable"
    old = {k: v for k, v in fill.items() if k not in ("reference_price", "used_book")}
    assert reconcile.replay_execution(book, old)[0] == "non vérifiable"


def test_decision_after_midnight_is_never_filled_at_the_candle_open():
    """La bougie ouvre à 100 € à 00:00:00 ; l'essai décide à 00:00:37 et lit le carnet."""
    order = PaperOrder("BTC/EUR", Side.BUY, 1.0, 100.0, 95.0)       # référence = ouverture
    moved = PaperBroker(1000.0, 0.0025, 10, None, None).buy(order, asks=[[100.3, 5.0]])
    assert moved["status"] == "filled" and moved["fill_price"] == pytest.approx(100.3)
    gone = PaperBroker(1000.0, 0.0025, 10, None, None).buy(order, asks=[[103.0, 5.0]])
    assert gone.get("fill_price", 103.0) >= 103.0                     # jamais 100 € rétroactif
