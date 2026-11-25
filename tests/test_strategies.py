from trading_agent.runner import closed_candles
from trading_agent.strategies import momentum_demo, momentum_exit


def candles_from(closes):
    return [[i * 60_000, c, c + 1, c - 1, c, 1.0] for i, c in enumerate(closes)]


def test_bullish_cross_gives_buy_signal_with_stop_below_price():
    signal = momentum_demo("BTC/EUR", candles_from([100.0] * 30 + [130.0]))
    assert signal is not None
    assert signal.stop_price < signal.reference_price == 130.0


def test_flat_market_gives_no_signal_and_no_exit():
    candles = candles_from([100.0] * 40)
    assert momentum_demo("BTC/EUR", candles) is None
    assert momentum_exit(candles) is False


def test_downtrend_triggers_exit():
    assert momentum_exit(candles_from([100.0] * 30 + [70.0])) is True


def test_unfinished_candle_is_dropped():
    candles = candles_from([100.0, 101.0, 102.0])  # bougies à 0, 60 s, 120 s
    kept = closed_candles(candles, tf_ms=60_000, now_ms=150_000)
    assert [c[0] for c in kept] == [0, 60_000]


# ---- indicateurs et nouvelles stratégies ----
from trading_agent.strategies import (  # noqa: E402
    STRATEGIES, atr, breakout_entry, breakout_exit, ema, get_strategy,
    reversion_entry, reversion_exit, rsi, sma, trend_entry, trend_exit)
import pytest  # noqa: E402


def test_indicators_on_known_values():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert ema([5.0] * 10, 3) == 5.0
    assert rsi([float(i) for i in range(20)]) == 100.0
    assert rsi([float(-i) for i in range(20)]) == 0.0
    assert atr(candles_from([100.0] * 20)) == 2.0   # haut = +1, bas = -1


def test_breakout_buys_new_high_in_uptrend_and_exits_on_new_low():
    rising = [100.0 + i * 0.1 for i in range(120)]
    assert breakout_entry("BTC/EUR", candles_from(rising)) is None      # hausse lente : pas de cassure nette
    signal = breakout_entry("BTC/EUR", candles_from(rising + [130.0]))
    assert signal.strategy == "breakout" and signal.stop_price < 130.0
    assert breakout_entry("BTC/EUR", candles_from([100.0] * 120)) is None
    assert breakout_exit(candles_from(rising + [80.0])) is True
    assert breakout_exit(candles_from(rising)) is False


def test_trend_requires_cross_and_long_uptrend():
    base = [100.0 + i * 0.05 for i in range(230)]
    dip = base + [105.0] * 40                  # la moyenne rapide passe sous la lente
    assert trend_exit(candles_from(dip)) is True
    restart = dip + [125.0]                    # nouveau départ, au-dessus de la moyenne longue
    assert trend_entry("BTC/EUR", candles_from(restart)).strategy == "trend"
    falling = [200.0 - i * 0.3 for i in range(260)] + [140.0]
    assert trend_entry("BTC/EUR", candles_from(falling)) is None   # rebond dans une baisse : refusé


def test_reversion_buys_turning_dip_only_in_uptrend():
    up = [100.0 + i * 0.5 for i in range(230)]
    dip = up + [up[-1] - 3 * k for k in range(1, 9)]       # creux marqué
    assert reversion_entry("BTC/EUR", candles_from(dip)) is None    # encore en chute
    turn = dip + [dip[-1] + 9.0]                           # le creux se retourne
    assert reversion_entry("BTC/EUR", candles_from(turn)).strategy == "reversion"
    assert reversion_exit(candles_from(up)) is True        # prix remonté : on sort
    down = [300.0 - i * 0.5 for i in range(230)]
    dip_down = down + [down[-1] - 3 * k for k in range(1, 9)]
    assert reversion_entry("BTC/EUR", candles_from(dip_down + [dip_down[-1] + 9.0])) is None


def test_registry():
    assert set(STRATEGIES) == {"momentum_demo", "breakout", "trend", "reversion",
                               "volume_expansion", "volatility_breakout", "tsmom", "pullback",
                               "volume_expansion_bear", "relative_strength", "compression_breakout",
                               "dip_rebound", "volume_breadth", "volume_leaders",
                               "volume_chandelier", "rsi2_rebound"}
    with pytest.raises(ValueError):
        get_strategy("inconnue")
