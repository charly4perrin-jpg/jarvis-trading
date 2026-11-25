import json
from datetime import datetime, timezone

import pytest

from trading_agent import tearsheet as ts

UTC = timezone.utc
SINCE = "2026-01-01T08:00:00+00:00"
NOW = datetime(2026, 1, 5, 20, 0, tzinfo=UTC)


def write_lab(tmp_path, fills, name="momo", raw_lines=()):
    (tmp_path / "since.txt").write_text(SINCE, encoding="utf-8")
    lines = [json.dumps(f) for f in fills] + list(raw_lines)
    (tmp_path / f"{name}_ledger.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def sell(day, pnl, hour=12):
    return {"type": "paper_fill", "side": "sell", "symbol": "BTC/EUR",
            "ts": f"2026-01-{day:02d}T{hour:02d}:00:00+00:00", "realized_pnl": pnl}


def test_spaced_curve_matches_hand_computed_ratios(tmp_path):
    # Capital 1000 ; +100 le 1er, -220 le 3, +132 le 5 ; rien les 2 et 4.
    # Courbe journalière : 1000 (avant), 1100, 1100, 880, 880, 1012
    # Rendements : 0,10 ; 0 ; -0,20 ; 0 ; 0,15 → moyenne 0,01
    # Variance (n-1) = (0,0081+0,0001+0,0441+0,0001+0,0196)/4 = 0,018
    # Sharpe = 0,01/√0,018 × √365 ≈ 1,42400 ; Sortino = 0,01/√(0,04/5) × √365 ≈ 2,13600
    # Volatilité = √0,018 × √365 ≈ 2,56319 ; annualisé = 1,012^73 − 1 ≈ 1,38883
    write_lab(tmp_path, [sell(1, 100.0), sell(3, -220.0), sell(5, 132.0)])
    r = ts.lab_report(tmp_path, "momo", 1000.0, NOW)
    m = r["metrics"]
    assert m["days"] == 5
    assert [v for _, v in r["curve"]] == pytest.approx([1000, 1100, 1100, 880, 880, 1012])
    assert m["return"] == pytest.approx(0.012)
    assert m["sharpe"] == pytest.approx(1.42400, abs=1e-4)
    assert m["sortino"] == pytest.approx(2.13600, abs=1e-4)
    assert m["volatility"] == pytest.approx(2.56319, abs=1e-4)
    assert m["annual_return"] == pytest.approx(1.38883, abs=1e-4)
    assert m["max_drawdown"] == pytest.approx(0.20)
    assert m["max_drawdown_days"] == 3
    assert m["calmar"] == pytest.approx(1.38883 / 0.20, abs=1e-3)


def test_loss_on_start_day_counts(tmp_path):
    write_lab(tmp_path, [sell(1, -200.0, hour=9)])
    now = datetime(2026, 1, 1, 23, 0, tzinfo=UTC)
    r = ts.lab_report(tmp_path, "momo", 1000.0, now, closes={"2025-12-31": 50.0})
    m = r["metrics"]
    assert m["days"] == 1
    assert m["return"] == pytest.approx(-0.20)
    assert m["max_drawdown"] == pytest.approx(0.20)
    assert m["max_drawdown_days"] == 1
    assert r["buy_and_hold"]["return"] == pytest.approx(0.0)
    assert r["excess_return"] == pytest.approx(-0.20)


def test_buy_and_hold_forward_filled_from_close_before_start(tmp_path):
    # Base = clôture du 31/12 (100) ; 110 le 2, rien les 3-4, 99 le 5.
    # Valeurs : 1000, 1000, 1100, 1100, 1100, 990 → rendement -1 %, perte max 10 % sur 1 jour.
    write_lab(tmp_path, [sell(1, 100.0), sell(3, -220.0), sell(5, 132.0)])
    closes = {"2025-12-31": 100.0, "2026-01-02": 110.0, "2026-01-05": 99.0}
    r = ts.lab_report(tmp_path, "momo", 1000.0, NOW, closes=closes)
    bh = r["buy_and_hold"]
    assert bh["days"] == 5
    assert bh["return"] == pytest.approx(-0.01)
    assert bh["max_drawdown"] == pytest.approx(0.10)
    assert bh["max_drawdown_days"] == 1
    assert r["excess_return"] == pytest.approx(0.022)
    assert ts.NOTE_MIXED in r["notes"] and ts.NOTE_REALIZED in r["notes"]
    assert "réalisé" in ts.render(r)


def test_no_close_before_start_means_no_benchmark(tmp_path):
    write_lab(tmp_path, [sell(2, 10.0)])
    r = ts.lab_report(tmp_path, "momo", 1000.0, NOW, closes={"2026-01-02": 100.0})
    assert r["buy_and_hold"] is None and r["excess_return"] is None
    assert ts.NOTE_MIXED not in r["notes"]


def test_naive_now_and_naive_trade_date_refused(tmp_path):
    write_lab(tmp_path, [sell(2, 10.0)])
    with pytest.raises(ValueError):
        ts.lab_report(tmp_path, "momo", 1000.0, datetime(2026, 1, 5, 20, 0))
    write_lab(tmp_path, [{"side": "sell", "ts": "2026-01-02T12:00:00", "realized_pnl": 5.0}])
    with pytest.raises(ValueError):
        ts.lab_report(tmp_path, "momo", 1000.0, NOW)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), True, "5", None])
def test_invalid_realized_pnl_refuses_the_strategy(tmp_path, bad):
    write_lab(tmp_path, [sell(2, 10.0), sell(3, bad)])
    out = ts.lab_reports(tmp_path, ["momo"], 1000.0, NOW)
    assert "error" in out["momo"]
    assert "refusé" in ts.render(out["momo"])


def test_non_object_lines_ignored_and_counted(tmp_path):
    write_lab(tmp_path, [sell(2, 10.0)], raw_lines=["[1, 2]", "42", "pas du json"])
    r = ts.lab_report(tmp_path, "momo", 1000.0, NOW)
    assert r["ignored_lines"] == 3
    assert r["trades"]["trades"] == 1
    assert any("3 ligne(s)" in n for n in r["notes"])


def test_sells_before_since_and_buys_are_left_out(tmp_path):
    old = {"side": "sell", "ts": "2025-12-30T12:00:00+00:00", "realized_pnl": -500.0}
    buy = {"side": "buy", "ts": "2026-01-02T12:00:00+00:00"}
    write_lab(tmp_path, [old, buy, sell(2, 30.0)])
    r = ts.lab_report(tmp_path, "momo", 1000.0, NOW)
    assert r["trades"]["trades"] == 1
    assert r["metrics"]["return"] == pytest.approx(0.03)
    assert r["metrics"]["max_drawdown"] == 0.0 and r["metrics"]["calmar"] is None


def test_trade_distribution_by_hand():
    d = ts.trade_distribution([10.0, 20.0, -5.0, -5.0, -5.0, 0.0, 30.0])
    assert (d["trades"], d["wins"], d["losses"], d["flat"]) == (7, 3, 3, 1)
    assert d["net"] == pytest.approx(45.0)
    assert d["median"] == 0.0 and d["best"] == 30.0 and d["worst"] == -5.0
    assert d["average_win"] == pytest.approx(20.0) and d["average_loss"] == pytest.approx(-5.0)
    assert d["payoff"] == pytest.approx(4.0) and d["profit_factor"] == pytest.approx(4.0)
    assert d["max_consecutive_wins"] == 2 and d["max_consecutive_losses"] == 3


def write_shadow(tmp_path, closed, since="2026-01-01T08:00:00+00:00"):
    path = tmp_path / "shadow.json"
    path.write_text(json.dumps({"open": [], "closed": closed, "reference": {
        "since": since, "start_equity": 1000.0, "prices": {"BTC/EUR": 100.0}}}), encoding="utf-8")
    return path


def shadow_trade(day, r, blocked_by, hour=12):
    return {"symbol": "BTC/EUR", "blocked_by": blocked_by, "r": r,
            "closed_at": f"2026-01-{day:02d}T{hour:02d}:00:00+00:00"}


def test_shadow_report_uses_capped_risk_and_counts_start_day(tmp_path):
    # Risque 1 % de 1000 € = 10 € par R : -2R le 1er (début) = -20 €, +3R le 3 = +30 €.
    path = write_shadow(tmp_path, [shadow_trade(1, -2.0, "regime", hour=9), shadow_trade(3, 3.0, "risk"),
                                   shadow_trade(4, -1.0, "regime"),
                                   {**shadow_trade(1, 5.0, "regime"), "closed_at": "2025-12-31T12:00:00+00:00"}])
    before = path.read_bytes()
    r = ts.shadow_report(path, 1000.0, 0.01, NOW, closes={"2025-12-31": 100.0})
    assert path.read_bytes() == before
    assert [v for _, v in r["curve"]] == pytest.approx([1000, 980, 980, 1010, 1000, 1000])
    assert r["metrics"]["return"] == pytest.approx(0.0)
    assert r["metrics"]["max_drawdown"] == pytest.approx(0.02)
    assert r["trades"]["trades"] == 3
    by = r["by_blocker"]
    assert by["regime"]["trades"] + by["risk"]["trades"] == r["trades"]["trades"]
    assert by["regime"]["net"] + by["risk"]["net"] == pytest.approx(r["trades"]["net"])
    assert by["regime"]["net"] == pytest.approx(-30.0)


@pytest.mark.parametrize("risk", [0.02, 0.0, -0.01, float("nan"), True])
def test_shadow_risk_out_of_bounds_refused(tmp_path, risk):
    path = write_shadow(tmp_path, [shadow_trade(2, 1.0, "regime")])
    with pytest.raises(ValueError):
        ts.shadow_report(path, 1000.0, risk, NOW)


@pytest.mark.parametrize("bad", [float("nan"), True, None])
def test_shadow_invalid_r_refused(tmp_path, bad):
    path = write_shadow(tmp_path, [shadow_trade(2, bad, "regime")])
    with pytest.raises(ValueError):
        ts.shadow_report(path, 1000.0, 0.01, NOW)


def test_shadow_naive_now_refused(tmp_path):
    path = write_shadow(tmp_path, [shadow_trade(2, 1.0, "regime")])
    with pytest.raises(ValueError):
        ts.shadow_report(path, 1000.0, 0.01, datetime(2026, 1, 5))
