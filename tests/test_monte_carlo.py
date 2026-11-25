"""Robustesse Monte-Carlo : intervalles de rendement et de perte maximale, refus si douteux."""
import json
import math

from trading_agent.monte_carlo import (MAX_RUNS, format_robustness, lab_robustness, percentile,
                                       simulate, trades_from_fills)
from trading_agent.strategies import LIVE_STRATEGIES as STRATEGIES


def sell(pnl, fee=1.0, entry_fee=1.0, qty=1.0, entry=1000.0, exit_=None):
    return {"side": "sell", "realized_pnl": pnl, "fee": fee, "entry_fee": entry_fee,
            "quantity": qty, "entry_price": entry, "fill_price": exit_ or entry + pnl}


def trades(pnls, **kw):
    return trades_from_fills([sell(p, **kw) for p in pnls])


def test_trades_from_fills_keeps_sells_only():
    fills = [{"side": "buy", "fee": 1.0}, sell(10.0, fee=1.0, entry_fee=2.0, qty=2.0, entry=100.0, exit_=105.0)]
    assert trades_from_fills(fills) == [{"pnl": 10.0, "fees": 3.0, "notional": 410.0}]


def test_trades_from_fills_rejects_incoherent_sells():
    assert trades_from_fills([sell(float("nan"))]) is None
    assert trades_from_fills([sell(5.0, fee=-1.0)]) is None
    assert trades_from_fills([{"side": "sell", "realized_pnl": 3.0}]) is None
    assert trades_from_fills([sell(True)]) is None


def test_percentile_interpolates():
    assert percentile([0.0, 10.0], 0.5) == 5.0
    assert percentile([1.0, 2.0, 3.0], 0.0) == 1.0
    assert percentile([1.0, 2.0, 3.0], 1.0) == 3.0


def test_interval_brackets_median_and_is_reproducible():
    t = trades([30, -20, 15, -10, 25, -5, 40, -30, 10, 5, -15, 20])
    a = simulate(t, 10_000, runs=500, seed=7)
    b = simulate(t, 10_000, runs=500, seed=7)
    assert a == b and a["status"] == "ok" and a["trades"] == 12
    assert a["return"]["low"] <= a["return"]["median"] <= a["return"]["high"]
    assert 0 <= a["max_drawdown"]["low"] <= a["max_drawdown"]["median"] <= a["max_drawdown"]["high"] < 1
    assert 0 <= a["prob_loss"] <= 1 and a["prob_ruin"] == 0
    assert math.isclose(a["observed_return"], 65 / 10_000)


def test_shuffle_without_noise_keeps_final_return_but_varies_drawdown():
    t = trades([50, 50, 50, -60, -60, -60, 40, 40, 40, 30])
    r = simulate(t, 1000, runs=300, seed=1, method="shuffle", fee_low=1.0, fee_high=1.0,
                 extra_slippage_bps=0.0)
    assert math.isclose(r["return"]["low"], r["return"]["high"])
    assert math.isclose(r["return"]["median"], r["observed_return"])
    assert r["max_drawdown"]["high"] > r["max_drawdown"]["low"]


def test_cost_noise_only_lowers_results():
    t = trades([20] * 12)
    clean = simulate(t, 1000, runs=200, seed=3, fee_low=1.0, fee_high=1.0, extra_slippage_bps=0.0)
    noisy = simulate(t, 1000, runs=200, seed=3, fee_low=1.0, fee_high=2.0, extra_slippage_bps=20.0)
    assert noisy["return"]["high"] < clean["return"]["low"]


def test_losing_strategy_has_high_loss_probability_and_ruin_is_counted():
    lose = simulate(trades([-5, -8, 2, -4, -6, 1, -7, -3, -9, 3]), 1000, runs=300, seed=2)
    assert lose["prob_loss"] > 0.9 and lose["return"]["high"] < 0
    ruin = simulate(trades([-400] * 10, entry=1000.0), 1000, runs=50, seed=2)
    assert ruin["prob_ruin"] == 1.0
    assert ruin["return"]["low"] == -1.0 and ruin["max_drawdown"]["high"] == 1.0


def test_fail_closed_refusals():
    t = trades([10] * 12)
    assert simulate(None, 1000)["status"] == "refusé"
    assert simulate(trades([10] * 3), 1000)["status"] == "insuffisant"
    assert simulate([], 1000, min_trades=0)["status"] == "insuffisant"
    for bad in ({"method": "magie"}, {"runs": 0}, {"runs": MAX_RUNS + 1}, {"runs": 10.5},
                {"fee_low": 2.0, "fee_high": 1.0}, {"extra_slippage_bps": -1},
                {"confidence": 1.0}, {"confidence": float("nan")}):
        r = simulate(t, 1000, **bad)
        assert r["status"] == "refusé" and "return" not in r, bad
    assert simulate(t, 0)["status"] == "refusé"
    assert simulate(t, float("inf"))["status"] == "refusé"


def test_lab_robustness_reads_each_strategy_ledger(tmp_path):
    names = list(STRATEGIES)
    good, broken = names[0], names[1]
    with (tmp_path / f"{good}_ledger.jsonl").open("w", encoding="utf-8") as f:
        for p in [10, -5, 8, -3, 12, -6, 7, 4, -2, 9]:
            f.write(json.dumps({"side": "buy", "fee": 1.0}) + "\n")
            f.write(json.dumps(sell(p)) + "\n")
    (tmp_path / f"{broken}_ledger.jsonl").write_text("{pas du json\n", encoding="utf-8")
    rows = {r["name"]: r for r in lab_robustness(str(tmp_path), 1000, runs=100)}
    assert set(rows) == set(names)
    assert rows[good]["status"] == "ok" and rows[good]["trades"] == 10
    assert rows[broken]["status"] == "refusé"
    for name in names[2:]:
        assert rows[name]["status"] == "insuffisant"
    text = format_robustness(list(rows.values()))
    assert "non concluant" in text and "rendement" in text


def test_invalid_counts_are_refused_not_coerced():
    t = trades([5.0, -3.0] * 20)
    for bad in ({"runs": True}, {"min_trades": "dix"}, {"seed": 1.5}, {"seed": False}):
        assert simulate(t, 3000.0, **bad)["status"] == "refusé"


def test_warnings_for_few_trades_fixed_euros_and_shuffle():
    few = simulate(trades([5.0, -3.0] * 6), 3000.0, runs=200)
    assert few["status"] == "ok" and any("moins de 30" in w for w in few["warnings"])
    assert any("euros fixes" in w for w in few["warnings"])
    many = simulate(trades([5.0, -3.0] * 20), 3000.0, runs=200, method="shuffle")
    assert not any("moins de 30" in w for w in many["warnings"])
    assert any("Ordre mélangé" in w for w in many["warnings"])
    assert "! Ordre mélangé" in format_robustness([{"name": "x", "label": "x", **many}])
