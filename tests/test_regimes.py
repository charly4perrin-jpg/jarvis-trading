import math

import pytest

from trading_agent.evaluate import regime_breakdown
from trading_agent.regimes import MIN_HISTORY, can_enter, classify, regime_series
from trading_agent.strategies import STRATEGIES


def series(closes, volume=10.0):
    return [[i * 3_600_000, c, c * 1.001, c * 0.999, c, volume] for i, c in enumerate(closes)]


def wave(n, drift):
    # Petite oscillation régulière autour d'une dérive : volatilité stable.
    return [100 * math.exp(drift * i) * (1 + 0.002 * math.sin(i)) for i in range(n)]


def test_trend_is_measured_from_moving_averages():
    assert classify(series(wave(400, 0.002)))["trend"] == "haussier"
    assert classify(series(wave(400, -0.002)))["trend"] == "baissier"
    assert classify(series(wave(400, 0.0)))["trend"] == "sans tendance"


def test_short_history_is_unknown_and_blocks_entry():
    regime = classify(series(wave(MIN_HISTORY - 1, 0.002)))
    assert regime is None
    assert can_enter(STRATEGIES["breakout"], regime)[0] is False


def test_regime_uses_only_the_past():
    candles = series(wave(300, 0.001) + wave(100, -0.004))
    full = regime_series(candles)
    for i in (MIN_HISTORY, 280, 330, 399):
        assert regime_series(candles[: i + 1])[i] == full[i]


def test_exceptional_move_and_thin_volume_block_entry():
    closes = wave(400, 0.002)
    shock = series(closes[:-1] + [closes[-2] * 1.2])
    regime = classify(shock)
    assert regime["shock"] is True
    assert can_enter(STRATEGIES["breakout"], regime) == (False, "mouvement exceptionnel sur la dernière bougie")
    thin = series(closes)
    thin[-1][5] = 0.5
    assert can_enter(STRATEGIES["trend"], classify(thin))[1] == "volume anormalement faible"


def test_high_volatility_is_detected_and_respected_per_strategy():
    closes = wave(380, 0.002)
    closes += [closes[-1] * (1 + 0.04 * (-1) ** i + 0.004) for i in range(20)]
    regime = classify(series(closes))
    assert regime["volatility"] == "haute"
    regime = {**regime, "shock": False, "trend": "haussier"}
    assert can_enter(STRATEGIES["breakout"], regime)[0] is True
    assert can_enter(STRATEGIES["reversion"], regime)[1] == "volatilité trop haute pour cette stratégie"


def test_bear_market_is_off_limits_for_every_strategy():
    # Seule exception, déclarée et testée à part : la variante en essai volume_expansion_bear.
    regime = {"trend": "baissier", "volatility": "normale", "shock": False, "thin": False}
    allowed = {n for n, s in STRATEGIES.items() if can_enter(s, regime)[0]}
    assert allowed == {"volume_expansion_bear"}


def test_breakdown_requires_both_halves_to_agree():
    up = {"trend": "haussier", "volatility": "normale"}
    flat = {"trend": "sans tendance", "volatility": "normale"}
    trades = ([{"pnl": 2.0, "regime": up, "half": h} for h in (0, 1) for _ in range(12)]
              + [{"pnl": 3.0, "regime": flat, "half": 0} for _ in range(12)]
              + [{"pnl": -1.0, "regime": flat, "half": 1} for _ in range(12)])
    rows = {(r["axis"], r["value"]): r for r in regime_breakdown(trades)}
    assert rows["trend", "haussier"]["verdict"] == "positif sur les deux moitiés"
    assert rows["trend", "sans tendance"]["verdict"] == "instable d'une moitié à l'autre"
    assert rows["trend", "baissier"]["verdict"] == "trop peu de trades pour conclure"
    assert rows["volatility", "normale"]["trades"] == 48


@pytest.mark.asyncio
async def test_analyst_declines_signal_outside_regime(tmp_path):
    from tests.test_agents import BUY, FakeExchange
    from trading_agent.agents import Coordinator
    from trading_agent.config import Settings
    from trading_agent.events import EventBus
    from trading_agent.paper import PaperBroker

    bus = EventBus(str(tmp_path / "events.jsonl"))
    broker = PaperBroker(1000.0, 0.0025, 10, None, str(tmp_path / "state.json"))
    coordinator = Coordinator(bus, Settings(_env_file=None, regime_filter=True), broker)
    corr = await coordinator.run_cycle(FakeExchange(BUY), "fake", "BTC/EUR", "1m")
    events = bus.thread(corr)
    types = [e.type for e in events]
    assert "regime.detected" in types and "trade.proposal" not in types
    blocked = next(e for e in events if e.type == "strategy.no_signal")
    assert blocked.payload["regime_blocked"] is True
    assert not broker.portfolio.positions
