"""Phase B : base de données des événements."""
import sqlite3
from datetime import datetime, timedelta, timezone

from trading_agent.agents import ExecutionAgent
from trading_agent.events import Event, EventBus
from trading_agent.models import PaperOrder, Side
from trading_agent.paper import PaperBroker
from trading_agent.store import EventStore


def publish(bus, type_, corr="c1", source=None, **payload):
    source = source or {"cycle": "coordinator", "market": "market_intelligence",
                        "risk": "risk_manager"}[type_.split(".")[0]]
    return bus.publish(Event(source=source, target="auditor", type=type_, correlation_id=corr,
                             result=f"{type_} {payload.get('symbol', '')}", payload=payload))


def test_every_event_lands_in_journal_and_database(tmp_path):
    bus = EventBus(str(tmp_path / "events.jsonl"), db_path=str(tmp_path / "jarvis.db"))
    publish(bus, "cycle.start")
    publish(bus, "market.snapshot", symbol="BTC/EUR")
    publish(bus, "market.snapshot", corr="c2", symbol="ETH/EUR")
    store = bus.store
    assert store.count() == 3 and bus.recording_ok
    assert [e["correlation_id"] for e in store.search(type_prefix="market.")] == ["c2", "c1"]
    assert store.search(symbol="ETH/EUR")[0]["payload"]["symbol"] == "ETH/EUR"
    assert len(store.search(correlation_id="c1")) == 2
    assert store.search(text="BTC")[0]["symbol"] == "BTC/EUR"
    assert store.compare_with_journal(bus.path)["consistent"]


def test_existing_journal_is_imported_once(tmp_path):
    journal = tmp_path / "events.jsonl"
    bus = EventBus(str(journal))
    for _ in range(4):
        publish(bus, "cycle.start")
    db = str(tmp_path / "jarvis.db")
    assert EventBus(str(journal), db_path=db).store.count() == 4
    again = EventBus(str(journal), db_path=db)          # redémarrage : pas de doublons
    assert again.store.count() == 4 and again.store.sync_from_journal(journal) == 0


def test_tampered_database_row_is_detected(tmp_path):
    bus = EventBus(str(tmp_path / "events.jsonl"), db_path=str(tmp_path / "jarvis.db"))
    publish(bus, "cycle.start")
    bus.store.db.execute("UPDATE events SET hash = 'faux'")
    bus.store.db.commit()
    report = bus.store.compare_with_journal(bus.path)
    assert report["altered"] == 1 and not report["consistent"]


def test_database_outage_blocks_buys_then_catches_up(tmp_path, monkeypatch):
    bus = EventBus(str(tmp_path / "events.jsonl"), db_path=str(tmp_path / "jarvis.db"))
    store = bus.store
    real_insert = store.insert

    def broken(event):
        raise sqlite3.OperationalError("disque plein")
    monkeypatch.setattr(store, "insert", broken)
    bus.publish(Event(source="risk_manager", target="execution", type="risk.decision",
                      correlation_id="c", payload={"approved": True, "proposal_id": "p1"}))
    assert not bus.recording_ok and store.count() == 0
    assert (tmp_path / "events.jsonl").read_text().count("\n") == 1   # le journal, lui, est écrit
    execution = ExecutionAgent(bus, PaperBroker(1000.0, 0.0025, 10, None, None))
    proposal = {"proposal_id": "p1",
                "valid_until": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()}
    result = execution.open(PaperOrder("BTC/EUR", Side.BUY, 0.5, 100.0, 95.0), 0, "c", proposal=proposal)
    assert result["status"] == "rejected" and "base de données" in result["reason"]
    monkeypatch.setattr(store, "insert", real_insert)
    publish(bus, "cycle.start")                        # la base revient : rattrapage
    assert bus.recording_ok
    assert store.compare_with_journal(bus.path)["consistent"] and store.count() == 3


def test_stats_summarise_history(tmp_path):
    store = EventStore(str(tmp_path / "jarvis.db"))
    assert store.stats()["events"] == 0
    bus = EventBus(str(tmp_path / "events.jsonl"), db_path=str(tmp_path / "jarvis.db"))
    publish(bus, "cycle.start")
    stats = bus.store.stats()
    assert stats["events"] == 1 and stats["threads"] == 1 and stats["by_type"] == {"cycle.start": 1}


def test_concurrent_reads_do_not_mix_up(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    bus = EventBus(str(tmp_path / "events.jsonl"), db_path=str(tmp_path / "jarvis.db"))
    for i in range(30):
        publish(bus, "market.snapshot", corr=f"c{i}", symbol="BTC/EUR")
    store = bus.store

    def read(_):
        rows = store.search(type_prefix="market.", limit=30)
        store.stats()
        return all(isinstance(r["payload"], dict) for r in rows) and len(rows) == 30
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(read, range(200)))
