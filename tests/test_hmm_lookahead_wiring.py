"""Branchements : régime de Markov (information + veto « agité » confirmé seulement) et
analyse lookahead affichée en lecture seule."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.test_tearsheet_api import client
from trading_agent import agents
from trading_agent.agents import MarketSnapshot, RegimeDetector, StrategyAnalyst
from trading_agent.events import EventBus
from trading_agent.models import Side, Signal
from trading_agent.strategies import STRATEGIES

AGITE = {"state": "agité", "confident": True, "prob": 0.93}
DOUTE = {"state": "indéterminé", "confident": False, "prob": 0.55}


def fake_strategy(exit_=False):
    signal = Signal("BTC/EUR", Side.BUY, 0.8, 100.0, 95.0, "test", datetime.now(timezone.utc))
    return SimpleNamespace(name="momentum_demo", label="momentum", high_vol=False, exit_reason="test",
                           no_signal="rien", entry=lambda symbol, candles: signal,
                           exit=lambda candles: exit_)


def analyst(tmp_path, monkeypatch, hmm_filter=True, exit_=False):
    assert not STRATEGIES["momentum_demo"].high_vol
    monkeypatch.setattr(agents, "effective", lambda name, params: fake_strategy(exit_))
    monkeypatch.setattr(agents, "can_enter", lambda s, r: (True, "régime compatible"))
    bus = EventBus(str(tmp_path / "events.jsonl"))
    return bus, StrategyAnalyst(bus, "momentum_demo", True, None, None, hmm_filter)


def snap(hmm):
    return MarketSnapshot("fake", "BTC/EUR", "1h", [[0, 1, 1, 1, 1, 1]], 1.0, 5.0, regime={}, hmm=hmm)


def test_confirmed_agitated_phase_blocks_a_calm_strategy(tmp_path, monkeypatch):
    bus, a = analyst(tmp_path, monkeypatch)
    outcome, _ = a.analyze(snap(AGITE), False, "c")
    assert outcome == "blocked" and bus.events[-1].payload["hmm_blocked"]
    assert "phase agitée confirmée" in bus.events[-1].result


@pytest.mark.parametrize("hmm", [None, DOUTE, {"state": "calme", "confident": True, "prob": 0.9}])
def test_uncertain_or_calm_reading_never_blocks(tmp_path, monkeypatch, hmm):
    _, a = analyst(tmp_path, monkeypatch)
    assert a.analyze(snap(hmm), False, "c")[0] == "signal"


def test_filter_can_be_switched_off(tmp_path, monkeypatch):
    _, a = analyst(tmp_path, monkeypatch, hmm_filter=False)
    assert a.analyze(snap(AGITE), False, "c")[0] == "signal"


def test_exits_are_never_blocked_by_markov(tmp_path, monkeypatch):
    _, a = analyst(tmp_path, monkeypatch, exit_=True)
    assert a.analyze(snap(AGITE), True, "c", "momentum_demo")[0] == "exit"


def test_regime_event_carries_markov_reading_and_is_computed_once_per_candle(tmp_path, monkeypatch):
    calls = []
    import trading_agent.hmm_regime as hm
    monkeypatch.setattr(hm, "hmm_regime", lambda c: calls.append(1) or DOUTE)
    bus = EventBus(str(tmp_path / "events.jsonl"))
    det = RegimeDetector(bus)
    s = snap(None)
    det.detect(s, "c")
    det.detect(s, "c2")
    assert len(calls) == 1 and s.hmm == DOUTE
    assert bus.events[-1].payload["hmm"] == DOUTE and "Markov : indéterminé" in bus.events[-1].result


def test_markov_crash_is_an_unknown_reading(tmp_path, monkeypatch):
    import trading_agent.hmm_regime as hm
    monkeypatch.setattr(hm, "hmm_regime", lambda c: 1 / 0)
    det = RegimeDetector(EventBus(str(tmp_path / "events.jsonl")))
    s = snap(None)
    det.detect(s, "c")
    assert s.hmm is None


def test_lookahead_endpoint_reports_every_strategy_and_caches(tmp_path):
    with client(tmp_path) as c:
        first = c.get("/api/lookahead").json()
        assert first["error"] is None and set(first["reports"]) == set(STRATEGIES) | {"regimes"}
        assert first["status"] in ("sain", "insuffisant", "fuite", "non_reproductible", "erreur")
        assert c.get("/api/lookahead").json()["computed_at"] == first["computed_at"]


def test_recursive_endpoint_reports_every_strategy_and_caches(tmp_path):
    with client(tmp_path) as c:
        first = c.get("/api/recursive").json()
        assert first["error"] is None and set(first["reports"]) == set(STRATEGIES)
        assert c.get("/api/recursive").json()["computed_at"] == first["computed_at"]
