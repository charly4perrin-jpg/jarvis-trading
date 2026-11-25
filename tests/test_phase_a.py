"""Phase A : contrat d'événement v2, parcours complet, Trade Manager, contradicteur, scanner."""
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_agents import BUY, FakeExchange
from trading_agent.agents import Coordinator, ExecutionAgent
from trading_agent.config import Settings
from trading_agent.events import PERMISSIONS, Event, EventBus, parse_line, verify_chain
from trading_agent.models import PaperOrder, Side
from trading_agent.paper import PaperBroker


def settings(**kw):
    return Settings(_env_file=None, **{"regime_filter": False, **kw})


def make(tmp_path, **kw):
    bus = EventBus(str(tmp_path / "events.jsonl"))
    broker = PaperBroker(1000.0, 0.0025, 10, None, str(tmp_path / "state.json"))
    return bus, broker, Coordinator(bus, settings(**kw), broker)


def test_new_events_carry_version_and_priority_and_old_journal_still_verifies(tmp_path):
    path = tmp_path / "events.jsonl"
    # Ligne du journal d'origine : ni version, ni priorité, ni réponse.
    old = Event(source="coordinator", target="coordinator", type="cycle.start",
                correlation_id="x", schema_version=1)
    old.hash = old.compute_hash()
    path.write_text(old.model_dump_json(exclude={"priority", "reply_to", "schema_version"}) + "\n")
    bus = EventBus(str(path))
    new = bus.publish(Event(source="coordinator", target="auditor", type="cycle.end",
                            correlation_id="x", priority="high"))
    assert new.schema_version == 2 and new.prev_hash == old.hash
    assert parse_line(path.read_text().splitlines()[0]).schema_version == 1
    assert verify_chain(str(path)) == (True, 2, None)


def test_payload_contract_is_enforced(tmp_path):
    bus = EventBus(str(tmp_path / "events.jsonl"))
    with pytest.raises(ValueError, match="trade.proposal"):
        bus.publish(Event(source="trade_proposer", target="risk_manager", type="trade.proposal",
                          correlation_id="x", payload={"symbol": "BTC/EUR"}))
    with pytest.raises(ValueError):
        Event(source="a", target="b", type="t", correlation_id="x", priority="urgent")


def test_registry_outputs_match_permissions(tmp_path):
    _, _, coordinator = make(tmp_path)
    for agent in coordinator.agents:
        info = agent.describe()
        assert info["role"] and info["outputs"], agent.name
        for type_ in info["outputs"]:
            allowed = next(who for prefix, who in PERMISSIONS.items() if type_.startswith(prefix))
            assert agent.name in allowed, (agent.name, type_)


@pytest.mark.asyncio
async def test_full_path_links_proposal_to_risk_answer(tmp_path):
    bus, broker, coordinator = make(tmp_path)
    corr = await coordinator.run_cycle(FakeExchange(BUY), "fake", "BTC/EUR", "1m")
    events = {e.type: e for e in bus.thread(corr)}
    proposal = events["trade.proposal"]
    assert proposal.payload["proposal_id"] and proposal.payload["valid_until"]
    assert proposal.payload["gain_if_target_per_unit"] < proposal.payload["take_profit"] - proposal.payload["entry_price"]
    assert events["risk.decision"].reply_to == proposal.id
    assert events["execution.fill"].payload["proposal_id"] == proposal.payload["proposal_id"]
    assert events["adversarial.review"].payload["verdict"] == "pass"


def _proposal(minutes: float = 2) -> dict:
    now = datetime.now(timezone.utc)
    return {"proposal_id": "p1", "valid_until": (now + timedelta(minutes=minutes)).isoformat()}


def _approve(bus, pid="p1", ok=True):
    bus.publish(Event(source="risk_manager", target="execution", type="risk.decision",
                      correlation_id="c", status="ok" if ok else "rejected",
                      payload={"approved": ok, "proposal_id": pid}))


def test_execution_refuses_expired_duplicate_and_refused_proposals(tmp_path):
    bus = EventBus(str(tmp_path / "events.jsonl"))
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    execution = ExecutionAgent(bus, broker)
    order = PaperOrder("BTC/EUR", Side.BUY, 0.5, 100.0, 95.0)
    assert execution.open(order, 0, "c")["reason"].startswith("aucune validation")
    _approve(bus)
    assert "expirée" in execution.open(order, 0, "c", proposal=_proposal(-1))["reason"]
    assert execution.open(order, 0, "c", proposal=_proposal())["status"] == "filled"
    broker.close_position("BTC/EUR", 100.0, reason="test")
    assert execution.open(order, 0, "c", proposal=_proposal())["reason"] == "proposition déjà exécutée."
    _approve(bus, "p2", ok=False)
    _approve(bus, "p2", ok=True)   # un refus est définitif
    assert not bus.is_approved("p2")


@pytest.mark.asyncio
async def test_major_objection_stops_the_path_before_any_proposal(tmp_path):
    bus, broker, coordinator = make(tmp_path, adversarial_min_depth_eur=1_000_000)
    corr = await coordinator.run_cycle(FakeExchange(BUY), "fake", "BTC/EUR", "1m")
    types = [e.type for e in bus.thread(corr)]
    review = next(e for e in bus.thread(corr) if e.type == "adversarial.review")
    assert review.status == "rejected" and review.payload["verdict"] == "reject"
    assert any(o["code"] == "liquidite" and o["severity"] == "majeure"
               for o in review.payload["objections"])
    assert "trade.proposal" not in types and not broker.portfolio.positions


def test_critical_anomaly_halts_the_system(tmp_path):
    bus, _, _ = make(tmp_path)
    bus.publish(Event(source="execution", target="security_monitor", type="anomaly.report",
                      correlation_id="c", priority="critical", result="solde incohérent"))
    assert bus.halted


class ScanExchange(FakeExchange):
    async def markets(self):
        return {s: {"quote": "EUR", "spot": True, "active": True}
                for s in ("BTC/EUR", "ETH/EUR", "USDC/EUR", "THIN/EUR", "SOL/EUR")}

    async def tickers(self, symbols=None):
        volume = {"BTC/EUR": 9e7, "ETH/EUR": 5e7, "USDC/EUR": 8e7, "THIN/EUR": 7e7, "SOL/EUR": 2e7}
        return {s: {"last": 100.0, "bid": 99.99, "ask": 100.01, "quoteVolume": v}
                for s, v in volume.items()}

    async def order_book(self, symbol, limit=50):
        size = 1.0 if symbol == "THIN/EUR" else 1_000.0
        return {"bids": [[99.99, size]], "asks": [[100.01, size]]}


@pytest.mark.asyncio
async def test_scanner_picks_liquid_pairs_without_stablecoins(tmp_path):
    bus, _, coordinator = make(tmp_path, watchlist_size=3)
    watchlist = await coordinator.scan(ScanExchange(BUY), "fake", "SOL/EUR")
    assert watchlist == ["SOL/EUR", "BTC/EUR", "ETH/EUR"]
    assert bus.events[-1].type == "market.scan"


@pytest.mark.asyncio
async def test_scanner_failure_falls_back_to_main_pair(tmp_path):
    bus, _, coordinator = make(tmp_path)
    assert await coordinator.scan(FakeExchange(BUY), "fake", "BTC/EUR") == ["BTC/EUR"]
    assert bus.events[-1].type == "agent.error"
