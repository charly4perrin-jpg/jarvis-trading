"""Candidates figées (docs/protocoles/candidates-luna.md) : chaque règle vérifiée sur des
bougies construites à la main, avant toute évaluation."""
import pytest

from trading_agent.backtest import run_backtest
from trading_agent.config import Settings
from trading_agent.strategies import (LIVE_STRATEGIES, STRATEGIES, compression_entry, compression_exit_with,
                                      dip_entry, dip_exit_with, relative_strength_table)

D = 86_400_000


def c(i, o, h, lo, cl, v=100.0):
    return [i * D, o, h, lo, cl, v]


def rising(n=100, start=60.0, end=95.0):
    out = []
    for i in range(n):
        p = start + (end - start) * i / (n - 1)
        out.append(c(i, p, p * 1.01, p * 0.99, p))
    return out


# --- cassure après compression ---------------------------------------------------------

def compression(last_close=103.0, width=1.0):
    base = rising()
    n = len(base)
    tight = [c(n + k, 100, 100 + width, 100 - width, 100) for k in range(10)]
    return base + tight + [c(n + 10, 100, last_close + 0.5, 99.5, last_close)]


def test_compression_breakout_enters_and_memorises_B():
    s = compression_entry("X", compression())
    assert s is not None and s.memo == {"B": 101.0} and s.reference_price == 103.0 and s.stop_price < 103.0


@pytest.mark.parametrize("candles", [compression(last_close=101.0),          # égal à B : pas strict
                                     compression(width=5.0)])                 # amplitude 10 % > 8 %
def test_compression_breakout_needs_strict_break_of_a_tight_range(candles):
    assert compression_entry("X", candles) is None


def test_compression_exit_under_B_or_on_the_10th_day():
    pos = {"entry_ts": 0, "memo": {"B": 101.0}}
    assert compression_exit_with([c(0, 1, 1, 1, 102), c(1, 1, 1, 1, 100.5)], pos)
    assert not compression_exit_with([c(7, 1, 1, 1, 105), c(8, 1, 1, 1, 105)], pos)
    assert compression_exit_with([c(8, 1, 1, 1, 105), c(9, 1, 1, 1, 105)], pos)   # vente à l'ouverture du jour 10


# --- rebond après une chute --------------------------------------------------------------

def dip(drop_close=94.0, last_low=93.5):
    base = rising(end=90.0)
    n = len(base)
    return base + [c(n, 100, 100, 100, 100), c(n + 1, 99, 99, 93, drop_close),
                   c(n + 2, 94, 97.5, last_low, 97)]


def test_dip_rebound_enters_and_memorises_the_previous_low():
    s = dip_entry("X", dip())
    assert s is not None and s.memo == {"low_prev": 93.0}


@pytest.mark.parametrize("candles", [dip(drop_close=96.0),      # chute de 4 % seulement
                                     dip(last_low=92.0)])        # nouveau plus bas
def test_dip_rebound_refusals(candles):
    assert dip_entry("X", candles) is None


def test_dip_exit_under_previous_low_or_on_the_5th_day():
    pos = {"entry_ts": 0, "memo": {"low_prev": 93.0}}
    assert dip_exit_with([c(0, 1, 1, 1, 95), c(1, 1, 1, 1, 92.9)], pos)
    assert not dip_exit_with([c(2, 1, 1, 1, 95), c(3, 1, 1, 1, 95)], pos)
    assert dip_exit_with([c(3, 1, 1, 1, 95), c(4, 1, 1, 1, 95)], pos)


# --- force relative ------------------------------------------------------------------------

def series(growth, n=200):
    return [c(i, 100 * (1 + growth) ** i, 100 * (1 + growth) ** i, 100 * (1 + growth) ** i,
              100 * (1 + growth) ** i) for i in range(n)]


def test_ranking_needs_every_pair_and_breaks_ties_alphabetically():
    h = {"BBB": series(0.002), "AAA": series(0.002), "CCC": series(0.001)}
    table = relative_strength_table(h)
    row = table[150 * D]
    assert row["AAA"][0] == 1 and row["BBB"][0] == 2 and row["CCC"][0] == 3
    h["CCC"] = h["CCC"][:120]                                   # une paire sans donnée ensuite
    assert 150 * D not in relative_strength_table(h)


def test_relative_strength_enters_top3_and_exits_at_rank_5():
    growths = {"A": 0.004, "B": 0.003, "C": 0.002, "D": 0.001, "E": 0.0005, "F": -0.001}
    h = {s: series(g) for s, g in growths.items()}
    strat = STRATEGIES["relative_strength"].universe(h)
    assert strat.entry("A", h["A"][:150]) is not None           # rang 1, hausse, au-dessus de la SMA 100
    assert strat.entry("D", h["D"][:150]) is None               # rang 4
    assert strat.exit_with(h["E"][:150], {"symbol": "E"})       # rang 5 : sortie
    assert not strat.exit_with(h["B"][:150], {"symbol": "B"})


def test_unbound_relative_strength_never_enters():
    assert STRATEGIES["relative_strength"].entry("A", series(0.004)) is None


# --- intégration au backtest et barrières --------------------------------------------------

def test_time_exit_sells_at_the_open_of_the_10th_day():
    candles = compression() + [c(111 + k, 104, 105, 103.5, 104) for k in range(15)]
    s = Settings(_env_file=None, regime_filter=False, adversarial_in_backtest=False, hmm_filter=False)
    r = run_backtest(candles, s, "X/EUR", 86_400, window=300, strategy=STRATEGIES["compression_breakout"])
    buy = next(f for f in r["fills"] if f["side"] == "buy")
    sell = next(f for f in r["fills"] if f["side"] == "sell")
    assert sell["ts_ms"] - buy["ts_ms"] == 10 * D and sell["reason"] == "exit_signal"


def test_candidates_never_run_live():
    assert not {"relative_strength", "compression_breakout", "dip_rebound"} & set(LIVE_STRATEGIES)
    with pytest.raises(ValueError):
        Settings(_env_file=None, strategy="dip_rebound").validate_safety()


def test_portfolio_backtest_binds_the_basket_and_uses_position_aware_exits():
    from trading_agent.portfolio_backtest import run_portfolio
    s = Settings(_env_file=None, regime_filter=False, adversarial_in_backtest=False, hmm_filter=False)
    candles = compression() + [c(111 + k, 104, 105, 103.5, 104) for k in range(15)]
    r = run_portfolio({"X/EUR": candles}, s, 86_400, ["compression_breakout"], window=300, windows=1)
    buy = next(f for f in r["fills"] if f["side"] == "buy")
    sell = next(f for f in r["fills"] if f["side"] == "sell")
    assert sell["ts_ms"] - buy["ts_ms"] == 10 * D               # même sortie au 10e jour qu'en backtest simple
    growths = {"A/EUR": 0.004, "B/EUR": 0.003, "C/EUR": 0.002, "D/EUR": 0.001}
    h = {k: series(g) for k, g in growths.items()}
    rs = run_portfolio(h, s, 86_400, ["relative_strength"], window=300, windows=1)
    assert any(f["side"] == "buy" for f in rs["fills"])         # liée au panier : elle peut entrer
