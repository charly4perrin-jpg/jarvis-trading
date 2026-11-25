"""Propriétés des verrous de sécurité, vérifiées sur des séquences aléatoires."""
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from trading_agent.agents import ExecutionAgent
from trading_agent.events import PERMISSIONS, Event, EventBus, verify_chain
from trading_agent.models import PaperOrder, Side
from trading_agent.paper import PaperBroker

CHECKS = hsettings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
AGENTS = sorted({a for who in PERMISSIONS.values() for a in who} | {"intrus"})
TYPES = sorted({p + "x" if p.endswith(".") else p for p in PERMISSIONS}) + ["inconnu.type", "trade.proposal"]


def allowed(source: str, type_: str) -> bool:
    who = next((w for prefix, w in PERMISSIONS.items() if type_.startswith(prefix)), None)
    return who is not None and source in who


@CHECKS
@given(st.lists(st.tuples(st.sampled_from(AGENTS), st.sampled_from(TYPES)), min_size=1, max_size=25))
def test_bus_enforces_permissions_and_keeps_an_intact_chain(messages):
    with tempfile.TemporaryDirectory() as tmp:
        bus = EventBus(str(Path(tmp) / "e.jsonl"), str(Path(tmp) / "HALT"), str(Path(tmp) / "j.db"))
        published = 0
        for source, type_ in messages:
            payload = {}
            if type_ == "trade.proposal":
                payload = {"proposal_id": "p", "symbol": "BTC/EUR", "side": "buy", "entry_price": 1.0,
                           "stop_price": 0.9, "take_profit": 1.2, "created_at": "x", "valid_until": "y",
                           "strategy": "s"}
            event = Event(source=source, target="auditor", type=type_, correlation_id="c", payload=payload)
            try:
                bus.publish(event)
                published += 1
                assert allowed(source, type_), (source, type_)          # rien d'interdit ne passe
            except PermissionError:
                assert not allowed(source, type_), (source, type_)      # rien d'autorisé n'est bloqué
            except ValueError:
                pass                                                    # contrat de données refusé
        intact, count, _ = verify_chain(str(bus.path))
        assert intact and count == published
        assert bus.store.compare_with_journal(bus.path)["consistent"]


conditions = st.fixed_dictionaries({
    "approved": st.booleans(), "refused_after": st.booleans(), "already_executed": st.booleans(),
    "expired": st.booleans(), "halted": st.booleans(), "db_down": st.booleans(), "no_id": st.booleans()})


@CHECKS
@given(conditions)
def test_a_purchase_happens_only_when_every_condition_holds(c):
    with tempfile.TemporaryDirectory() as tmp:
        bus = EventBus(str(Path(tmp) / "e.jsonl"), str(Path(tmp) / "HALT"), str(Path(tmp) / "j.db"))
        broker = PaperBroker(10_000.0, 0.0025, 10, None, None)
        execution = ExecutionAgent(bus, broker)
        pid = None if c["no_id"] else "p1"
        valid = datetime.now(timezone.utc) + (timedelta(seconds=-1) if c["expired"] else timedelta(minutes=2))
        proposal = {"proposal_id": pid, "valid_until": valid.isoformat()}

        def decide(ok):
            bus.publish(Event(source="risk_manager", target="execution", type="risk.decision", correlation_id="c",
                              status="ok" if ok else "rejected", payload={"approved": ok, "proposal_id": pid}))
        if pid and c["approved"]:
            decide(True)
        if pid and c["refused_after"]:
            decide(False)
        if pid and c["already_executed"]:
            bus.publish(Event(source="execution", target="portfolio_manager", type="execution.fill",
                              correlation_id="c", payload={"proposal_id": pid}))
        if c["halted"]:
            bus.publish(Event(source="coordinator", target="coordinator", type="system.halt", correlation_id="c"))
        if c["db_down"]:
            def broken(event):
                raise sqlite3.OperationalError("panne")
            bus.store.insert = broken
            bus.publish(Event(source="coordinator", target="auditor", type="cycle.start", correlation_id="c"))
        order = PaperOrder("BTC/EUR", Side.BUY, 0.01, 100.0, 95.0)
        result = execution.open(order, 0, "c", proposal=proposal)
        everything_ok = (pid and c["approved"] and not c["refused_after"] and not c["already_executed"]
                         and not c["expired"] and not c["halted"] and not c["db_down"])
        assert (result["status"] == "filled") == bool(everything_ok), (c, result)
        if result["status"] == "filled":
            again = execution.open(order, 0, "c", proposal=proposal)       # jamais deux fois
            assert again["status"] == "rejected"
