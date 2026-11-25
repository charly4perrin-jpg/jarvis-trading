"""Backtest de portefeuille : capital partagé, mêmes règles qu'en direct, sans triche."""
import math

from trading_agent.config import Settings
from trading_agent.portfolio_backtest import run_portfolio

D = 86_400_000


def series(n, drift, phase, vol=0.03):
    out, p = [], 100.0
    for i in range(n):
        p *= math.exp(drift + vol * math.sin(i / 5 + phase) * 0.3)
        v = 30.0 if i % 23 == 0 else 10.0
        out.append([i * D, p * 0.995, p * 1.01, p * 0.99, p, v])
    return out


def settings(**kw):
    return Settings(_env_file=None, **{"regime_filter": False, **kw})


def test_no_look_ahead_prefix_is_identical():
    h = {"AAA/EUR": series(400, 0.002, 0), "BBB/EUR": series(400, 0.001, 2), "CCC/EUR": series(400, 0.0015, 4)}
    full = run_portfolio(h, settings(), 86_400, ["momentum_demo", "breakout"])
    cut = 280
    short = run_portfolio({k: v[:cut] for k, v in h.items()}, settings(), 86_400, ["momentum_demo", "breakout"])
    assert full["equity_curve"][:cut - 1] == short["equity_curve"][:cut - 1]
    assert full["math_ok"] and full["trades"] > 0


def test_shared_capital_respects_portfolio_limits():
    h = {f"S{i}/EUR": series(400, 0.002, i) for i in range(8)}
    r = run_portfolio(h, settings(max_open_positions=3), 86_400, ["momentum_demo"])
    assert r["peak_positions"] <= 3
    assert min(r["equity_curve"]) > 0 and r["average_exposure"] <= 0.6 + 0.05


def test_perfectly_correlated_markets_count_as_one_risk():
    same = series(400, 0.002, 0)
    h = {f"S{i}/EUR": [row[:] for row in same] for i in range(5)}
    r = run_portfolio(h, settings(), 86_400, ["momentum_demo"])
    # 0,5 % de risque par trade, 1 % au plus pour des positions liées : 2 positions au plus.
    assert r["peak_positions"] <= 2
    assert r["refusals"].get("Correlated risk limit reached.", 0) > 0


def test_orders_waiting_for_next_open_count_in_position_limit():
    h = {f"S{i}/EUR": series(400, 0.003, i * 0.7, vol=0.02) for i in range(12)}
    r = run_portfolio(h, settings(max_open_positions=5, max_correlated_risk_pct=0.5), 86_400, ["momentum_demo"])
    assert r["peak_positions"] <= 5
