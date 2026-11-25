import math
from datetime import datetime, timedelta, timezone

import pytest

from trading_agent.config import Settings
from trading_agent.evaluate import Registry, evaluate_strategy, paper_evidence


def series(n=900, drift=0.02):
    closes = [100 + 15 * math.sin(i / 12) + i * drift for i in range(n)]
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append([i * 3_600_000, o, max(o, c) + 0.3, min(o, c) - 0.3, c, 1.0])
    return out


def settings(**kw):
    return Settings(_env_file=None, **{"regime_filter": False, **kw})


def histories():
    return {"AAA/EUR": {"candles": series(), "limits": None, "fee": 0.0025},
            "BBB/EUR": {"candles": series(drift=0.05), "limits": None, "fee": 0.0025}}


def test_evaluation_pools_markets_and_windows():
    r = evaluate_strategy(histories(), settings(), 3600, "momentum_demo", windows=6)
    assert r["symbols"] == ["AAA/EUR", "BBB/EUR"] and len(r["per_symbol"]) == 2
    assert len(r["window_returns"]) == 6 and sum(r["window_trades"]) == r["trades"] > 0
    assert [c["key"] for c in r["criteria"]] == ["trades", "expectancy", "windows", "profit_factor", "drawdown"]
    assert r["passed"] == all(c["passed"] for c in r["criteria"])


def test_thresholds_decide_the_verdict():
    strict = evaluate_strategy(histories(), settings(accept_min_trades=10_000), 3600, "momentum_demo")
    assert not strict["passed"] and not strict["criteria"][0]["passed"]
    loose = evaluate_strategy(histories(), settings(accept_min_trades=1), 3600, "momentum_demo")
    assert loose["criteria"][0]["passed"]


def passing(name="breakout", days_old=0):
    when = datetime.now(timezone.utc) - timedelta(days=days_old)
    return {"kind": "backtest", "strategy": name, "passed": True, "evaluated_at": when.isoformat(),
            "criteria": [{"label": "x", "value": "ok", "passed": True}]}


def test_promotion_needs_valid_fresh_evidence_and_a_person(tmp_path):
    reg = Registry(str(tmp_path / "r.json"), str(tmp_path / "ev"))
    assert reg.status("breakout") == "candidate"
    with pytest.raises(PermissionError, match="Aucune évaluation"):
        reg.promote("breakout", None, by="test")
    failing = {**passing(), "passed": False,
               "criteria": [{"label": "Au moins 100 trades", "value": "3", "passed": False}]}
    with pytest.raises(PermissionError, match="Au moins 100 trades"):
        reg.promote("breakout", failing, by="test")
    with pytest.raises(PermissionError, match="7 jours"):
        reg.promote("breakout", passing(days_old=8), by="test")
    with pytest.raises(PermissionError, match="Aucune évaluation"):
        reg.promote("breakout", passing(name="trend"), by="test")      # preuve d'une autre stratégie
    with pytest.raises(PermissionError, match="personne"):
        reg.promote("breakout", passing(), by="")
    assert reg.promote("breakout", passing(), by="test") == "backtest_ok"


def test_promotion_goes_one_step_at_a_time_and_is_persisted(tmp_path):
    reg = Registry(str(tmp_path / "r.json"), str(tmp_path / "ev"))
    reg.promote("breakout", passing(), by="test")
    with pytest.raises(PermissionError):                     # une preuve backtest ne vaut pas preuve papier
        reg.promote("breakout", passing(), by="test")
    assert Registry(str(tmp_path / "r.json"), str(tmp_path / "ev")).status("breakout") == "backtest_ok"
    assert reg.history("breakout")[0]["by"] == "test"
    assert reg.demote("breakout", "test", "performance dégradée") == "candidate"
    assert len(reg.history("breakout")) == 2


def test_paper_evidence_requires_time_trades_and_profit():
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    row = {"name": "breakout", "trades": 25, "return": 0.03}
    young = paper_evidence(row, (now - timedelta(weeks=2)).isoformat(), now)
    assert not young["passed"] and not young["criteria"][0]["passed"]
    ready = paper_evidence(row, (now - timedelta(weeks=9)).isoformat(), now)
    assert ready["passed"]
    losing = paper_evidence({**row, "return": -0.01}, (now - timedelta(weeks=9)).isoformat(), now)
    assert not losing["passed"]


def test_agents_cannot_change_the_registry(tmp_path):
    from trading_agent.events import Event, EventBus
    bus = EventBus(str(tmp_path / "e.jsonl"))
    for agent in ("strategy_analyst", "risk_manager", "execution", "auditor"):
        with pytest.raises(PermissionError):
            bus.publish(Event(source=agent, target="coordinator", type="registry.change",
                              correlation_id="x"))


def test_deflated_sharpe_is_an_extra_gate_for_promotion():
    from trading_agent.evaluate import add_deflated_criterion
    import random
    rng = random.Random(3)
    weak = [rng.gauss(0.0002, 0.01) for _ in range(800)]
    trials = [0.02, -0.01, 0.015, 0.03, 0.0]
    report = {"criteria": [{"key": "x", "label": "x", "value": "ok", "passed": True}], "passed": True}
    add_deflated_criterion(report, weak, trials, 30, 0.95)
    assert report["criteria"][-1]["key"] == "deflated" and not report["passed"]
    strong = [rng.gauss(0.003, 0.01) for _ in range(800)]
    report2 = {"criteria": [], "passed": True}
    add_deflated_criterion(report2, strong, trials, 30, 0.95)
    assert report2["passed"] and report2["deflated_sharpe"]["dsr"] > 0.95
