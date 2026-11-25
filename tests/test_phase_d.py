"""Phase D : nouvelles stratégies, Performance Analyst, dégradation et pause."""
import math

import pytest

from tests.test_agents import FakeExchange
from tests.test_server import make_client, wait_for
from trading_agent.agents import Coordinator, PerformanceAnalyst
from trading_agent.config import Settings
from trading_agent.events import EventBus
from trading_agent.paper import PaperBroker
from trading_agent.performance import PauseList, curve_stats, degradation, trade_stats
from trading_agent.strategies import squeeze_entry, volume_entry


def candles(closes, volumes=None, spread=0.5):
    volumes = volumes or [10.0] * len(closes)
    out, prev = [], closes[0]
    for i, (c, v) in enumerate(zip(closes, volumes)):
        out.append([i * 3_600_000, prev, max(prev, c) + spread, min(prev, c) - spread, c, v])
        prev = c
    return out


def test_volume_expansion_needs_unusual_volume_and_strong_close():
    closes = [100 + 0.05 * i + math.sin(i) for i in range(150)]
    base = candles(closes + [closes[-1] + 3], [10.0] * 150 + [35.0], spread=0.1)
    assert volume_entry("BTC/EUR", base).strategy == "volume_expansion"
    quiet = candles(closes + [closes[-1] + 3], [10.0] * 151, spread=0.1)
    assert volume_entry("BTC/EUR", quiet) is None


def test_volatility_breakout_fires_after_compression_only():
    noisy = [100 + 4 * math.sin(i * 0.7) for i in range(130)]
    calm = [100 + 0.05 * math.sin(i) for i in range(30)]
    signal = squeeze_entry("BTC/EUR", candles(noisy + calm + [103.0], spread=0.1))
    assert signal is not None and signal.stop_price < 103.0
    assert squeeze_entry("BTC/EUR", candles(noisy + noisy[:31] + [103.0], spread=0.1)) is None


def test_trade_and_curve_statistics():
    stats = trade_stats([10.0, -5.0, 10.0, -5.0])
    assert stats["expectancy"] == 2.5 and stats["profit_factor"] == 2.0 and stats["win_rate"] == 0.5
    curve = curve_stats([("a", 100.0), ("b", 110.0), ("c", 99.0), ("d", 105.0)], 365)
    assert curve["max_drawdown"] == pytest.approx(0.1) and curve["sortino"] > curve["sharpe"] > 0


def test_degradation_rules():
    assert degradation([-5.0] * 9, None) == []                       # trop tôt pour juger
    assert "10 derniers" in degradation([3.0] * 5 + [-4.0] * 10, None)[0]
    below = degradation([1.0, -1.5] * 10, backtest_expectancy=4.0)
    assert any("backtest" in r for r in below)
    assert degradation([3.0, -1.0] * 10, backtest_expectancy=1.0) == []


def test_only_a_person_can_resume_a_paused_strategy(tmp_path):
    pauses = PauseList(str(tmp_path / "paused.json"))
    pauses.pause("breakout", "dégradée", by="performance_analyst")
    with pytest.raises(PermissionError):
        pauses.resume("breakout", by="")
    assert pauses.resume("breakout", by="interface")["reason"] == "dégradée"
    assert not pauses.is_paused("breakout")


def _settings(tmp_path, **kw):
    return Settings(_env_file=None, regime_filter=False, pause_path=str(tmp_path / "paused.json"),
                    performance_dir=str(tmp_path / "perf"), evaluations_dir=str(tmp_path / "ev"), **kw)


@pytest.mark.asyncio
async def test_paused_strategy_opens_nothing(tmp_path):
    from tests.test_agents import BUY
    settings = _settings(tmp_path)
    PauseList(settings.pause_path).pause("momentum_demo", "dégradée", by="test")
    bus = EventBus(str(tmp_path / "events.jsonl"))
    broker = PaperBroker(1000.0, 0.0025, 10, None, None)
    corr = await Coordinator(bus, settings, broker).run_cycle(FakeExchange(BUY), "fake", "BTC/EUR", "1m")
    blocked = next(e for e in bus.thread(corr) if e.type == "strategy.no_signal")
    assert blocked.payload["paused"] is True and not broker.portfolio.positions


def test_analyst_measures_and_pauses_a_degraded_strategy(tmp_path):
    settings = _settings(tmp_path)
    bus = EventBus(str(tmp_path / "events.jsonl"))
    broker = PaperBroker(1000.0, 0.0025, 10, str(tmp_path / "ledger.jsonl"), None)
    broker.portfolio.positions = {}
    with (tmp_path / "ledger.jsonl").open("w") as f:
        for i in range(12):
            f.write('{"side": "sell", "symbol": "BTC/EUR", "strategy": "breakout", "quantity": 1, '
                    f'"fill_price": 100, "slippage_bps": 10, "fee": 0.25, "realized_pnl": {-3 if i > 1 else 4}}}\n')
    analyst = PerformanceAnalyst(bus, settings)
    venues = [{"id": "bitvavo", "broker": broker, "start": 1000.0}]
    report = analyst.report(venues, 14_400, "p1")
    row = report["exchanges"][0]
    assert row["trades"]["trades"] == 12 and row["fees"] == pytest.approx(3.0)
    assert row["slippage"] == pytest.approx(1.2)
    assert analyst.pauses.is_paused("breakout")
    types = [e.type for e in bus.events]
    assert types == ["performance.report", "performance.degradation"]
    analyst.report(venues, 14_400, "p2")                       # pas de seconde pause
    assert [e.type for e in bus.events].count("performance.degradation") == 1


def test_resume_from_interface_is_journaled(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/api/performance").status_code == 404
        assert client.post("/api/strategies/resume", json={"strategy": "breakout"}).status_code == 404
        client.post("/api/cycle")
        wait_for(client, "performance.report")
        assert client.get("/api/performance").json()["exchanges"][0]["exchange"] == "fake"
        PauseList(str(tmp_path / "paused.json")).pause("breakout", "dégradée", by="performance_analyst")
        assert "breakout" in client.get("/api/state").json()["system"]["paused"]
        assert client.post("/api/strategies/resume", json={"strategy": "breakout"}).json()["paused"] == {}
        wait_for(client, "registry.change")
        assert (tmp_path / "perf" / "fake_equity.jsonl").exists()
