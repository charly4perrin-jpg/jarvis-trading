import math
import random
import time

import pytest

from tests.test_backtest import series, settings
from tests.test_hmm_lookahead_wiring import AGITE, DOUTE, analyst, snap
from trading_agent import backtest as bt
from trading_agent import hmm_regime as hm
from trading_agent.hmm_regime import hmm_blocks, hmm_regime, hmm_series
from trading_agent.lookahead import SAIN, analyze_series
from trading_agent.strategies import STRATEGIES


def candles_from(rets, start=100.0):
    p = start
    out = [[0, p, p, p, p, 1.0]]
    for r in rets:
        p *= math.exp(r)
        out.append([len(out) * 86_400_000, p, p, p, p, 1.0])
    return out


def regimes(seed, n, block=80, calm=0.006, wild=0.035):
    rng = random.Random(seed)
    return candles_from([rng.gauss(0, wild if (i // block) % 2 else calm) for i in range(n)])


# --- cohérence avec hmm_regime --------------------------------------------------

def test_refit_bars_match_hmm_regime_exactly():
    c = regimes(1, 160)
    out = hmm_series(c, window=100, refit_every=7)
    assert len(out) == len(c) and all(v is None for v in out[:100])
    refits = range(100, len(c), 7)
    for i in refits:
        assert out[i] == hmm_regime(c[:i + 1], window=100)
    assert any(out[i] is not None for i in refits)


def test_refit_every_bar_equals_hmm_regime_everywhere():
    c = regimes(2, 125)
    out = hmm_series(c, window=100, refit_every=1)
    assert out[100:] == [hmm_regime(c[:i + 1], window=100) for i in range(100, len(c))]


def test_reads_agitated_after_a_switch_and_same_keys_as_hmm_regime():
    rng = random.Random(3)
    c = candles_from([rng.gauss(0, 0.005) for _ in range(300)] + [rng.gauss(0, 0.03) for _ in range(120)])
    out = hmm_series(c)
    last = out[-1]
    assert last["state"] == "agité" and last["confident"]
    assert set(last) == set(hmm_regime(c))
    # Jamais d'étiquette sans confiance, entre deux réajustements aussi.
    assert all(r["confident"] == (r["state"] != "indéterminé") for r in out if r)


# --- causalité -----------------------------------------------------------------

def test_future_never_changes_the_past_lookahead_analysis():
    c = regimes(4, 190)
    report = analyze_series(lambda s: hmm_series(s, window=100, refit_every=7), c, checkpoints=4, seeds=2)
    assert report["status"] == SAIN, report


def test_prefix_gives_the_same_readings():
    c = regimes(5, 180)
    full = hmm_series(c, window=100, refit_every=5)
    for cut in (101, 133, 157):
        assert hmm_series(c[:cut], window=100, refit_every=5) == full[:cut]


# --- performance ---------------------------------------------------------------

def test_2000_daily_candles_under_60_seconds():
    c = regimes(6, 1999)
    started = time.perf_counter()
    out = hmm_series(c)
    elapsed = time.perf_counter() - started
    assert len(out) == 2000 and elapsed < 60, elapsed
    assert sum(r is not None for r in out) > 1500


# --- fail-closed ---------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [{"window": 99}, {"window": 250.0}, {"n_states": 4},
                                    {"refit_every": 0}, {"refit_every": True},
                                    {"refit_every": hm.MAX_REFIT_EVERY + 1}])
def test_bad_parameters_give_no_reading(kwargs):
    c = regimes(7, 140)
    assert hmm_series(c, **{"window": 100, **kwargs}) == [None] * len(c)


@pytest.mark.parametrize("candles", [None, "abc", 3])
def test_bad_container_gives_empty_list(candles):
    assert hmm_series(candles) == []


def test_bad_candle_blocks_readings_while_in_window():
    c = regimes(8, 260)
    c[150] = [c[150][0], 1, 1, 1, float("nan"), 1]
    out = hmm_series(c, window=100, refit_every=7)
    # La bougie 150 casse deux variations (149→150 et 150→151) : aucune lecture
    # tant que l'une d'elles est dans la fenêtre.
    assert all(out[i] is None for i in range(150, 252))
    assert any(out[i] is not None for i in range(100, 150))


def test_flat_market_gives_no_reading():
    c = [[i, 100, 100, 100, 100, 1] for i in range(140)]
    assert hmm_series(c, window=100) == [None] * 140


# --- veto identique au direct -------------------------------------------------

READINGS = [None, AGITE, DOUTE, {"state": "calme", "confident": True, "prob": 0.9},
            {"state": "agité", "confident": False, "prob": 0.95}, {}]


@pytest.mark.parametrize("hmm", READINGS)
@pytest.mark.parametrize("high_vol", [False, True])
def test_veto_matches_live_strategy_analyst(tmp_path, monkeypatch, hmm, high_vol):
    _, a = analyst(tmp_path, monkeypatch)
    from trading_agent import agents
    from tests.test_hmm_lookahead_wiring import fake_strategy
    fake = fake_strategy()
    fake.high_vol = high_vol
    monkeypatch.setattr(agents, "effective", lambda name, params: fake)
    live = a.analyze(snap(hmm), False, "c")[0] == "blocked"
    assert (hmm_blocks(fake, hmm) is not None) == live


# --- branchement dans le backtest --------------------------------------------

def test_backtest_follows_the_hmm_filter_setting(monkeypatch):
    """Le backtest suit le réglage HMM_FILTER du direct ; coupé, Markov n'est pas calculé."""
    def boom(*a, **k):
        raise AssertionError("hmm_series ne doit pas être appelé")
    monkeypatch.setattr(bt, "_HMM_CACHE", {})
    monkeypatch.setattr(bt, "hmm_series", boom)
    off = settings(regime_filter=True).model_copy(update={"hmm_filter": False})
    assert bt.run_backtest(series(), off, "BTC/EUR", 3600)["candles"] == 400
    calls = []
    monkeypatch.setattr(bt, "hmm_series", lambda c, refit_every=7: calls.append(1) or [None] * len(c))
    bt.run_backtest(series(), settings(regime_filter=True), "BTC/EUR", 3600)
    bt.run_backtest(series(), settings(regime_filter=True), "BTC/EUR", 3600)
    assert calls == [1]   # calculé une seule fois pour les mêmes bougies


def _agitated_everywhere(monkeypatch):
    monkeypatch.setattr(bt, "_HMM_CACHE", {})
    monkeypatch.setattr(bt, "can_enter", lambda s, r: (True, "régime compatible"))
    monkeypatch.setattr(bt, "hmm_series", lambda c, refit_every=7: [AGITE] * len(c))


def test_backtest_veto_blocks_calm_strategy(monkeypatch):
    _agitated_everywhere(monkeypatch)
    assert not STRATEGIES["momentum_demo"].high_vol
    r = bt.run_backtest(series(), settings(regime_filter=True), "BTC/EUR", 3600,
                        strategy="momentum_demo", hmm_filter=True)
    assert r["signals"] > 0 and not r["fills"]
    assert r["refusals"] == {"régime : modèle de Markov : phase agitée confirmée": r["signals"]}


def test_backtest_veto_spares_high_vol_strategy_and_needs_regime_filter(monkeypatch):
    _agitated_everywhere(monkeypatch)
    name = next(n for n, s in STRATEGIES.items() if s.high_vol)
    on = bt.run_backtest(series(), settings(regime_filter=True), "BTC/EUR", 3600, strategy=name, hmm_filter=True)
    off = bt.run_backtest(series(), settings(regime_filter=True), "BTC/EUR", 3600, strategy=name)
    assert on["equity_curve"] == off["equity_curve"]
    # Comme en direct, le veto de Markov n'existe que si le filtre de régime est actif.
    calm_on = bt.run_backtest(series(), settings(), "BTC/EUR", 3600, strategy="momentum_demo", hmm_filter=True)
    calm_off = bt.run_backtest(series(), settings(), "BTC/EUR", 3600, strategy="momentum_demo")
    assert calm_on["equity_curve"] == calm_off["equity_curve"]


def test_backtest_with_real_series_runs_and_stays_causal():
    c = regimes(9, 420, block=60)
    c = [[i * 3_600_000, x[4], x[4] * 1.002, x[4] * 0.998, x[4], 1.0] for i, x in enumerate(c)]
    full = bt.run_backtest(c, settings(regime_filter=True), "BTC/EUR", 3600, hmm_filter=True)["equity_curve"]
    part = bt.run_backtest(c[:330], settings(regime_filter=True), "BTC/EUR", 3600, hmm_filter=True)["equity_curve"]
    assert part == full[:330]
