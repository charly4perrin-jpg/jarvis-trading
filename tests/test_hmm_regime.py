import math
import random
import time

import pytest

from trading_agent import hmm_regime as hm
from trading_agent.hmm_regime import hmm_regime


def candles_from(rets, start=100.0):
    p = start
    out = [[0, p, p, p, p, 1.0]]
    for r in rets:
        p *= math.exp(r)
        out.append([len(out), p, p, p, p, 1.0])
    return out


def gauss(rng, n, sd):
    return [rng.gauss(0, sd) for _ in range(n)]


# --- lectures réelles ---------------------------------------------------------

@pytest.mark.parametrize("seed", range(4))
def test_calm_then_agitated_reads_agitated_with_confidence(seed):
    rng = random.Random(seed)
    c = candles_from(gauss(rng, 300, 0.005) + gauss(rng, 200, 0.03))
    r = hmm_regime(c)
    assert r["state"] == "agité" and r["confident"]
    assert r["separation"] >= hm.MIN_SEPARATION and r["bic_gain"] > 0
    assert r["sigmas"][0] < r["sigmas"][1]


@pytest.mark.parametrize("seed", range(4))
def test_agitated_then_calm_reads_calm(seed):
    rng = random.Random(100 + seed)
    r = hmm_regime(candles_from(gauss(rng, 300, 0.03) + gauss(rng, 200, 0.005)))
    assert r["state"] == "calme" and r["confident"]


@pytest.mark.parametrize("seed", range(6))
def test_homogeneous_market_is_never_confident(seed):
    """Marché homogène : l'étiquette calme/agité serait arbitraire -> refus."""
    rng = random.Random(200 + seed)
    r = hmm_regime(candles_from(gauss(rng, 500, 0.01)))
    assert r is not None
    assert r["confident"] is False
    assert r["state"] == "indéterminé"


def test_three_states_ordered_by_volatility():
    rng = random.Random(7)
    c = candles_from(gauss(rng, 200, 0.003) + gauss(rng, 200, 0.012) + gauss(rng, 200, 0.04))
    r = hmm_regime(c, window=600, n_states=3)
    assert r is not None
    assert r["sigmas"] == sorted(r["sigmas"])
    assert set(r["probs"]) == {"calme", "normal", "agité"}
    assert abs(sum(r["probs"].values()) - 1) < 1e-3
    assert r["state"] == "agité" and r["confident"]


def student_t(rng, n, scale, nu=3):
    """Rendements i.i.d. de Student-t (ν entier) : queues épaisses, sans régime."""
    out = []
    for _ in range(n):
        chi2 = sum(rng.gauss(0, 1) ** 2 for _ in range(nu))
        out.append(scale * rng.gauss(0, 1) / math.sqrt(chi2 / nu))
    return out


@pytest.mark.parametrize("seed", range(10))
def test_homogeneous_fat_tails_is_never_confident(seed):
    """Rendements indépendants à queues épaisses : le HMM peut séparer cœur et
    queues, mais sans persistance ce n'est pas un régime -> jamais confiant."""
    rng = random.Random(300 + seed)
    r = hmm_regime(candles_from(student_t(rng, 300, 0.01)))
    assert r is not None
    assert r["confident"] is False
    assert r["state"] == "indéterminé"


@pytest.mark.parametrize("seed", range(6))
def test_fat_tails_with_huge_last_candle_is_not_confident_agitated(seed):
    rng = random.Random(400 + seed)
    rets = student_t(rng, 299, 0.01) + [0.15]
    r = hmm_regime(candles_from(rets))
    assert r is not None
    assert not (r["confident"] and r["state"] == "agité")
    assert r["confident"] or r["state"] == "indéterminé"


def _fat_tail_false_alarms():
    hits = 0
    for seed in range(6):
        rets = student_t(random.Random(400 + seed), 299, 0.01) + [0.15]
        r = hmm_regime(candles_from(rets))
        hits += bool(r and r["confident"] and r["state"] == "agité")
    return hits


def test_without_both_guards_fat_tails_give_a_false_alarm(monkeypatch):
    """Témoin : sans persistance NI confirmation, un des cas ci-dessus serait
    lu « agité » confiant ; les cas testés ne sont donc pas triviaux."""
    monkeypatch.setattr(hm, "MIN_PERSISTENCE", 0.0)
    monkeypatch.setattr(hm, "CONFIRM_BARS", 1)
    assert _fat_tail_false_alarms() >= 1


@pytest.mark.parametrize("guard", ["persistance", "confirmation"])
def test_each_guard_alone_blocks_the_fat_tail_false_alarm(monkeypatch, guard):
    """Les deux garde-fous se recouvrent sur ces cas : chacun suffit seul.
    Ce test le constate (il ne prouve pas que chacun est indispensable)."""
    if guard == "persistance":
        monkeypatch.setattr(hm, "CONFIRM_BARS", 1)
    else:
        monkeypatch.setattr(hm, "MIN_PERSISTENCE", 0.0)
    assert _fat_tail_false_alarms() == 0


@pytest.mark.parametrize("seed", range(4))
def test_single_shock_after_calm_regime_is_not_confident_agitated(seed):
    """Vrais régimes, mais un seul choc final : la confirmation sur
    CONFIRM_BARS bougies empêche de lire « agité » confiant."""
    rng = random.Random(500 + seed)
    rets = gauss(rng, 100, 0.03) + gauss(rng, 199, 0.005) + [0.12]
    r = hmm_regime(candles_from(rets))
    assert r is not None
    assert not (r["confident"] and r["state"] == "agité")
    assert r["confident"] or r["state"] == "indéterminé"


def test_default_window_fits_in_300_candles():
    """Les agents chargent 300 bougies (moins la bougie en cours) : la fenêtre
    par défaut doit tenir dedans."""
    assert hm.DEFAULT_WINDOW + 1 <= 299
    rng = random.Random(21)
    c = candles_from(gauss(rng, 150, 0.005) + gauss(rng, 148, 0.03))
    assert len(c) == 299
    r = hmm_regime(c)
    assert r is not None and r["state"] == "agité" and r["confident"]


def test_occupancy_sums_to_one_and_states_persist():
    rng = random.Random(22)
    r = hmm_regime(candles_from(gauss(rng, 150, 0.005) + gauss(rng, 150, 0.03)))
    assert abs(sum(r["occupancy"]) - 1) < 1e-2
    assert all(p >= hm.MIN_PERSISTENCE for p in r["persistence"])


# --- causalité ----------------------------------------------------------------

def test_only_last_window_is_used():
    rng = random.Random(11)
    c = candles_from(gauss(rng, 300, 0.005) + gauss(rng, 400, 0.02))
    assert hmm_regime(c, window=200) == hmm_regime(c[-201:], window=200)


def test_future_candles_do_not_change_past_reading():
    """Lecture à l'instant k identique quel que soit le futur ; un choc futur
    n'apparaît que lorsqu'on le lit au moment où il a lieu."""
    rng = random.Random(12)
    base = gauss(rng, 400, 0.005)
    calm_future = candles_from(base + gauss(random.Random(1), 150, 0.005))
    wild_future = candles_from(base + gauss(random.Random(2), 150, 0.04))
    k = 401  # bougies 0..400 identiques dans les deux séries
    assert calm_future[:k] == wild_future[:k]
    before = hmm_regime(calm_future[:k], window=300)
    assert before == hmm_regime(wild_future[:k], window=300)
    assert before["state"] != "agité"
    after = hmm_regime(wild_future, window=300)
    assert after["state"] == "agité" and after["confident"]


# --- entrées invalides : None, jamais d'exception -----------------------------

GOOD = candles_from(gauss(random.Random(3), 300, 0.01))


@pytest.mark.parametrize("n_states", [2.0, True, False, 1, 4, "2", None, -2])
def test_bad_n_states_returns_none(n_states):
    assert hmm_regime(GOOD, window=200, n_states=n_states) is None


@pytest.mark.parametrize("window", [-1, 0, True, 200.0, "200", None, hm.MIN_WINDOW - 1, hm.MAX_WINDOW + 1])
def test_bad_window_returns_none(window):
    assert hmm_regime(GOOD, window=window) is None


def test_history_shorter_than_window_returns_none():
    assert hmm_regime(GOOD[:200], window=200) is None
    assert hmm_regime(GOOD[:201], window=200) is not None


@pytest.mark.parametrize("bad", [
    {"close": 100.0},
    [0, 1, 1, 1],
    [0, 1, 1, 1, float("nan"), 1],
    [0, 1, 1, 1, float("inf"), 1],
    [0, 1, 1, 1, 0.0, 1],
    [0, 1, 1, 1, -5.0, 1],
    [0, 1, 1, 1, "100", 1],
    [0, 1, 1, 1, True, 1],
    [0, 1, 1, 1, None, 1],
    "abcdef",
    None,
])
def test_bad_candle_in_window_returns_none(bad):
    c = list(GOOD)
    c[-5] = bad
    assert hmm_regime(c, window=200) is None


@pytest.mark.parametrize("candles", [None, "x", 42, {}, [], {"a": 1}])
def test_bad_container_returns_none(candles):
    assert hmm_regime(candles, window=200) is None


def test_constant_prices_returns_none():
    assert hmm_regime([[0, 1, 1, 1, 50.0, 1]] * 300, window=200) is None


def test_extreme_outlier_does_not_crash():
    rets = gauss(random.Random(5), 299, 0.01) + [5.0]
    r = hmm_regime(candles_from(rets), window=200)
    assert r is None or all(math.isfinite(s) for s in r["sigmas"])


def test_worst_case_runtime_is_bounded(monkeypatch):
    """Pire cas : fenêtre maximale, 3 états, convergence interdite (TOL < 0)
    donc les MAX_ITER itérations EM sont toutes faites."""
    monkeypatch.setattr(hm, "TOL", -1.0)
    rng = random.Random(9)
    rets = [rng.gauss(0, 0.01 * (1 + (i // 37) % 3)) for i in range(hm.MAX_WINDOW + 1)]
    t0 = time.perf_counter()
    r = hmm_regime(candles_from(rets), window=hm.MAX_WINDOW, n_states=3)
    elapsed = time.perf_counter() - t0
    assert r is not None
    assert elapsed < 30, elapsed


# --- étiquette jamais sans confiance -------------------------------------------

def _all_readings():
    rng = random.Random(77)
    series = [gauss(rng, 150, 0.005) + gauss(rng, 150, 0.03), gauss(rng, 300, 0.01),
              student_t(rng, 300, 0.01), gauss(rng, 100, 0.03) + gauss(rng, 199, 0.005) + [0.12],
              gauss(rng, 150, 0.03) + gauss(rng, 150, 0.005)]
    for k in (2, 3):
        for rets in series:
            r = hmm_regime(candles_from(rets), n_states=k)
            if r is not None:
                yield r


def test_label_only_when_confident():
    """Un faux « calme » pourrait autoriser des entrées : sans confiance,
    l'étiquette est toujours « indéterminé »."""
    readings = list(_all_readings())
    assert len(readings) >= 6
    for r in readings:
        assert (r["state"] != "indéterminé") == r["confident"]


def test_mostly_unchanged_closes_return_none():
    """Paire peu liquide : trop de clôtures identiques, pas de lecture."""
    rng = random.Random(6)
    rets = [0.0 if rng.random() < 0.4 else rng.gauss(0, 0.01) for _ in range(298)]
    assert hmm_regime(candles_from(rets)) is None


def test_state_stuck_at_variance_floor_is_not_a_regime():
    """Un état quasi immobile collé au plancher de variance est un artefact,
    pas un régime « calme » : lecture indéterminée."""
    rng = random.Random(5)
    r = hmm_regime(candles_from(gauss(rng, 150, 1e-7) + gauss(rng, 148, 0.01)))
    assert r is not None and r["state"] == "indéterminé" and not r["confident"]
