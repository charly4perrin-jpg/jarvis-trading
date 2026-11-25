"""Variante volume_expansion_bear : mêmes règles que volume_expansion, achats permis en
tendance baissière. Elle ne pilote pas l'essai principal."""
from trading_agent.config import Settings
from trading_agent.regimes import can_enter
from trading_agent.strategies import STRATEGIES

BEAR = {"trend": "baissier", "volatility": "normale", "shock": False, "thin": False}


def test_only_the_variant_may_enter_in_a_bearish_trend():
    assert can_enter(STRATEGIES["volume_expansion_bear"], BEAR)[0]
    assert not can_enter(STRATEGIES["volume_expansion"], BEAR)[0]


def test_variant_keeps_the_same_signals_and_safety_vetoes():
    base, bear = STRATEGIES["volume_expansion"], STRATEGIES["volume_expansion_bear"]
    assert bear.entry is base.entry and bear.exit is base.exit and bear.high_vol == base.high_vol
    assert not can_enter(bear, {**BEAR, "shock": True})[0]
    assert not can_enter(bear, {**BEAR, "thin": True})[0]
    assert not can_enter(bear, None)[0]


def test_main_trial_strategy_is_unchanged():
    assert Settings(_env_file=None).strategy != "volume_expansion_bear"
