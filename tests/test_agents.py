import json

import pytest

from trading_agent.agents import Coordinator
from trading_agent.config import Settings
from trading_agent.events import Event, EventBus, verify_chain
from trading_agent.models import PaperOrder, Side
from trading_agent.paper import PaperBroker


class FakeExchange:
    """Plateforme simulée : bougies d'une minute, toutes terminées."""

    def __init__(self, closes, fail=False):
        self.closes = closes
        self.fail = fail

    async def ohlcv(self, symbol, timeframe="1m", limit=100):
        if self.fail:
            raise ConnectionError("réseau coupé")
        return [[i * 60_000, c, c + 1, c - 1, c, 1.0] for i, c in enumerate(self.closes)]

    async def ticker(self, symbol):
        return {"bid": self.closes[-1] - 0.05, "ask": self.closes[-1] + 0.05}

    async def order_book(self, symbol, limit=50):
        last = self.closes[-1]
        return {"bids": [[last - 0.05, 100.0]], "asks": [[last + 0.05, 100.0]]}

    def limits(self, symbol):
        return {"min_amount": 0.0001, "min_cost": 5.0}

    def timeframe_seconds(self, timeframe):
        return 60

    def now_ms(self):
        return len(self.closes) * 60_000


BUY = [100.0] * 30 + [130.0]
FLAT = [100.0] * 40


def make(tmp_path):
    bus = EventBus(str(tmp_path / "events.jsonl"))
    broker = PaperBroker(1000.0, 0.0025, 10, str(tmp_path / "ledger.jsonl"),
                         str(tmp_path / "state.json"))
    return bus, broker, Coordinator(bus, Settings(_env_file=None, regime_filter=False), broker)


async def cycle(coordinator, closes, **kwargs):
    corr = await coordinator.run_cycle(FakeExchange(closes, **kwargs), "fake", "BTC/EUR", "1m")
    return corr, [e.type for e in coordinator.bus.thread(corr)]


@pytest.mark.asyncio
async def test_approved_trade_follows_every_mandatory_step(tmp_path):
    bus, broker, coordinator = make(tmp_path)
    corr, types = await cycle(coordinator, BUY)
    assert types == [
        "cycle.start", "exchange.status", "market.snapshot", "regime.detected", "portfolio.update",
        "strategy.signal", "market.order_book", "adversarial.review", "trade.proposal",
        "allocation.assessment", "risk.decision", "routing.plan",
        "execution.fill", "portfolio.update", "cycle.end", "audit.thread_recorded",
    ]
    assert "BTC/EUR" in broker.portfolio.positions
    assert all(e.correlation_id == corr and e.id and e.ts for e in bus.thread(corr))
    assert verify_chain(str(bus.path)) == (True, 16, None)


@pytest.mark.asyncio
async def test_risk_refusal_blocks_execution(tmp_path):
    bus, broker, coordinator = make(tmp_path)
    broker.mark({})
    broker.portfolio.day_start_equity = 2000.0  # perte du jour bien au-delà de 2 %
    corr, types = await cycle(coordinator, BUY)
    assert "execution.fill" not in types
    decision = next(e for e in bus.thread(corr) if e.type == "risk.decision")
    assert decision.status == "rejected" and "journalière" in decision.result
    assert broker.portfolio.positions == {}


@pytest.mark.asyncio
async def test_no_signal_means_no_proposal(tmp_path):
    _, broker, coordinator = make(tmp_path)
    _, types = await cycle(coordinator, FLAT)
    assert "strategy.no_signal" in types and "trade.proposal" not in types
    assert broker.portfolio.positions == {}


@pytest.mark.asyncio
async def test_open_position_is_closed_on_exit_signal(tmp_path):
    _, broker, coordinator = make(tmp_path)
    await cycle(coordinator, BUY)
    _, types = await cycle(coordinator, BUY + [129.0] * 3 + [128.0] * 30 + [110.0])
    assert "strategy.exit_signal" in types and "execution.fill" in types
    assert broker.portfolio.positions == {}


def test_agent_cannot_emit_another_agents_event(tmp_path):
    bus, _, coordinator = make(tmp_path)
    with pytest.raises(PermissionError):
        coordinator.strategy.emit("risk.decision", "execution", "x", "faux accord")
    with pytest.raises(PermissionError):
        bus.publish(Event(source="inconnu", target="execution", type="execution.fill",
                          correlation_id="x"))
    assert not bus.is_approved("x")


def test_execution_refuses_buy_without_risk_approval(tmp_path):
    bus, broker, coordinator = make(tmp_path)
    order = PaperOrder("BTC/EUR", Side.BUY, 0.001, 100.0, 95.0)
    result = coordinator.execution.open(order, 0, "fil-sans-accord")
    assert result["status"] == "rejected"
    assert broker.portfolio.positions == {}
    assert bus.thread("fil-sans-accord")[-1].type == "execution.rejected"


@pytest.mark.asyncio
async def test_repeated_errors_halt_the_system(tmp_path):
    bus, broker, coordinator = make(tmp_path)
    for _ in range(coordinator.security.max_error_cycles):
        _, types = await cycle(coordinator, BUY, fail=True)
        assert "agent.error" in types and "execution.fill" not in types
    assert bus.halted
    assert coordinator.market.status == "error"
    _, types = await cycle(coordinator, BUY)  # même avec un bon signal, plus rien ne part
    assert types == ["cycle.end"]
    assert broker.portfolio.positions == {}


@pytest.mark.asyncio
async def test_stopped_agent_blocks_the_cycle(tmp_path):
    _, broker, coordinator = make(tmp_path)
    coordinator.risk.enabled = False
    _, types = await cycle(coordinator, BUY)
    assert "agent.error" in types and "execution.fill" not in types
    assert coordinator.risk.status == "stopped"
    assert broker.portfolio.positions == {}


@pytest.mark.asyncio
async def test_tampered_journal_is_detected_and_chain_survives_restart(tmp_path):
    bus, broker, coordinator = make(tmp_path)
    await cycle(coordinator, FLAT)
    bus2 = EventBus(str(bus.path))  # redémarrage : la chaîne continue
    Coordinator(bus2, Settings(_env_file=None, regime_filter=False), broker).halt("test")
    intact, count, _ = verify_chain(str(bus.path))
    assert intact and count == 9

    lines = bus.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["result"] = "texte falsifié"
    lines[1] = json.dumps(row)
    bus.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert verify_chain(str(bus.path))[::2] == (False, 2)


@pytest.mark.asyncio
async def test_halt_survives_restart_until_resume(tmp_path):
    def new_bus():
        return EventBus(str(tmp_path / "events.jsonl"), str(tmp_path / "HALT"))

    broker = PaperBroker(1000.0, 0.0025, 10, str(tmp_path / "ledger.jsonl"))
    Coordinator(new_bus(), Settings(_env_file=None, regime_filter=False), broker).halt("test")

    restarted = Coordinator(new_bus(), Settings(_env_file=None, regime_filter=False), broker)
    assert restarted.bus.halted
    _, types = await cycle(restarted, BUY)
    assert types == ["cycle.end"] and broker.portfolio.positions == {}

    restarted.bus.publish(Event(source="coordinator", target="coordinator",
                                type="system.resume", correlation_id="manual"))
    assert not new_bus().halted
    _, types = await cycle(Coordinator(new_bus(), Settings(_env_file=None, regime_filter=False), broker), BUY)
    assert "execution.fill" in types


@pytest.mark.asyncio
async def test_stale_data_is_refused_by_risk(tmp_path):
    bus, broker, coordinator = make(tmp_path)
    ex = FakeExchange(BUY)
    ex.now_ms = lambda: len(BUY) * 60_000 + 10 * 60_000  # données vieilles de 10 minutes
    corr = await coordinator.run_cycle(ex, "fake", "BTC/EUR", "1m")
    decision = next(e for e in bus.thread(corr) if e.type == "risk.decision")
    assert decision.status == "rejected" and "périmées" in decision.result
    assert broker.portfolio.positions == {}


@pytest.mark.asyncio
async def test_same_candle_is_not_analysed_twice(tmp_path):
    bus, broker, coordinator = make(tmp_path)
    _, first = await cycle(coordinator, BUY)
    assert "execution.fill" in first
    broker.close_position("BTC/EUR", 129.0, reason="stop")   # position fermée entre deux cycles
    corr, second = await cycle(coordinator, BUY)             # même bougie : pas de rachat
    assert "strategy.signal" not in second and "execution.fill" not in second
    assert "déjà analysée" in bus.thread(corr)[-2].result
    assert broker.portfolio.positions == {}
    _, third = await cycle(coordinator, BUY + [131.0])       # nouvelle bougie : analyse normale
    assert any(t.startswith("strategy.") for t in third)


def test_wait_is_aligned_on_next_candle_close():
    from trading_agent.runner import seconds_to_next_candle
    assert seconds_to_next_candle(now_s=14_400 * 5 + 100, tf_seconds=14_400, delay=20) == 14_320
    assert seconds_to_next_candle(now_s=14_400 * 5, tf_seconds=14_400, delay=20) == 14_420


def test_two_programs_can_share_the_journal_and_the_halt(tmp_path):
    log, flag = str(tmp_path / "events.jsonl"), str(tmp_path / "HALT")
    server, cli = EventBus(log, flag), EventBus(log, flag)

    def say(bus, type_="cycle.start"):
        bus.publish(Event(source="coordinator", target="coordinator", type=type_, correlation_id="x"))

    say(server); say(cli); say(server); say(cli, "system.halt")
    assert server.halted                      # l'arrêt lancé ailleurs est vu tout de suite
    say(cli, "system.resume")
    assert not server.halted
    say(server)
    assert verify_chain(log) == (True, 6, None)


@pytest.mark.asyncio
async def test_sale_is_linked_to_the_proposal_that_opened_the_position(tmp_path):
    bus, broker, coordinator = make(tmp_path)
    corr = await cycle(coordinator, BUY)
    proposal = next(e for e in bus.thread(corr[0]) if e.type == "trade.proposal").payload["proposal_id"]
    assert broker.portfolio.positions["BTC/EUR"]["proposal_id"] == proposal
    corr2, _ = await cycle(coordinator, BUY + [60.0])          # chute sous le stop
    sale = next(e for e in bus.thread(corr2) if e.type == "execution.fill")
    assert sale.payload["side"] == "sell" and sale.payload["proposal_id"] == proposal
    assert sale.payload["entry_price"] > sale.payload["fill_price"]
