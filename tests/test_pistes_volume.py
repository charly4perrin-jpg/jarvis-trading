"""Pistes figées (docs/protocoles/pistes-volume-expansion.md) : règles vérifiées sur des
bougies construites à la main, avant toute évaluation."""
import pytest

from trading_agent.config import Settings
from trading_agent.strategies import LIVE_STRATEGIES, STRATEGIES, breadth_table, volume_entry

D = 86_400_000


def c(i, o, h, lo, cl, v=100.0):
    return [i * D, o, h, lo, cl, v]


def series(growth, n=160, spike_at=None):
    """Clôtures en croissance régulière ; bougie haussière à volume triple au jour `spike_at`."""
    out = []
    for i in range(n):
        p = 100 * (1 + growth) ** i
        if i == spike_at:
            out.append(c(i, p * 0.98, p * 1.001, p * 0.97, p, 300.0))
        else:
            out.append(c(i, p, p * 1.01, p * 0.99, p))
    return out


def test_breadth_counts_pairs_above_sma100_only_when_all_are_computable():
    h = {"A": series(0.002), "B": series(0.002), "C": series(-0.002)}
    assert breadth_table(h)[150 * D] == 2
    h["C"] = h["C"][:120]
    assert 150 * D not in breadth_table(h)


def test_volume_breadth_needs_five_of_eight_pairs_in_trend():
    up = {f"U{k}": series(0.002) for k in range(5)}
    down = {f"D{k}": series(-0.002) for k in range(3)}
    h = {**up, **down, "U0": series(0.002, spike_at=150)}
    assert volume_entry("U0", h["U0"][:151]) is not None          # signal de base présent
    sig = STRATEGIES["volume_breadth"].universe(h).entry("U0", h["U0"][:151])
    assert sig is not None and sig.strategy == "volume_breadth" and "5 cryptos sur 8" in sig.rationale
    h4 = {**h, "U4": series(-0.002)}                              # 4 sur 8 seulement
    assert STRATEGIES["volume_breadth"].universe(h4).entry("U0", h4["U0"][:151]) is None


def test_volume_leaders_needs_rank_1_to_4():
    growths = {"A": 0.005, "B": 0.004, "C": 0.003, "D": 0.0025, "E": 0.002, "F": 0.001}
    h = {s: series(g) for s, g in growths.items()}
    h["D"] = series(0.0025, spike_at=150)
    h["E"] = series(0.002, spike_at=150)
    strat = STRATEGIES["volume_leaders"].universe(h)
    sig = strat.entry("D", h["D"][:151])
    assert sig is not None and sig.strategy == "volume_leaders" and "Rang 4" in sig.rationale
    assert volume_entry("E", h["E"][:151]) is not None and strat.entry("E", h["E"][:151]) is None


def test_pistes_never_run_live_nor_enter_unbound():
    assert not {"volume_breadth", "volume_leaders"} & set(LIVE_STRATEGIES)
    assert STRATEGIES["volume_breadth"].entry("A", series(0.002, spike_at=159)) is None
    with pytest.raises(ValueError):
        Settings(_env_file=None, strategy="volume_leaders").validate_safety()


# --- pistes du 2026-11-24 (docs/protocoles/pistes-sortie-horizon-rsi2.md) ------------------

from trading_agent.strategies import chandelier_exit_with, rsi2_entry, rsi2_exit  # noqa: E402


def test_chandelier_entry_is_volume_expansion_renamed():
    candles = series(0.002, spike_at=150)[:151]
    sig = STRATEGIES["volume_chandelier"].entry("A", candles)
    assert sig is not None and sig.strategy == "volume_chandelier"
    assert sig.stop_price == volume_entry("A", candles).stop_price


def test_chandelier_exit_follows_the_highest_high_since_entry():
    candles = [c(i, 100, 101, 99, 100) for i in range(30)]       # ATR(22) = 2
    pos = {"entry_ts": 25 * D}
    assert not chandelier_exit_with(candles, pos)
    candles[27] = c(27, 100, 110, 99, 100)                        # plus haut 110 depuis l'achat
    candles[-1] = c(29, 100, 101, 99, 102)                       # ATR(22) = 53 / 22 ≈ 2,41
    assert chandelier_exit_with(candles, pos)                    # 102 < 110 − 3 × 2,41
    assert not chandelier_exit_with(candles, {"entry_ts": None})
    candles[27], candles[20] = c(27, 100, 101, 99, 100), c(20, 100, 110, 99, 100)
    assert not chandelier_exit_with(candles, pos)                # plus haut AVANT l'achat : ignoré


def test_rsi2_enters_after_two_down_days_above_sma200_and_exits_above_sma5():
    up = [c(i, 100 + i * 0.5, 101 + i * 0.5, 99 + i * 0.5, 100 + i * 0.5) for i in range(201)]
    dip = up + [c(201, 200, 200, 196, 197), c(202, 197, 197, 193, 194)]
    sig = rsi2_entry("A", dip)
    assert sig is not None and sig.strategy == "rsi2_rebound" and sig.stop_price < 194
    assert rsi2_entry("A", up) is None                            # pas de baisse : RSI 2 = 100
    assert rsi2_entry("A", dip[-150:]) is None                    # moins de 201 bougies
    assert not rsi2_exit(dip) and rsi2_exit(dip + [c(203, 194, 200, 194, 199)])


def test_new_pistes_never_run_live():
    assert not {"volume_chandelier", "rsi2_rebound"} & set(LIVE_STRATEGIES)
