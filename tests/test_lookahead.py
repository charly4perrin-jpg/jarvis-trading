import math
import random

import pytest

from trading_agent.lookahead import (
    ERREUR, FUITE, INSTABLE, INSUFFISANT, SAIN, analyze_all, analyze_decision, analyze_series,
    analyze_strategy, perturb_future)
from trading_agent.models import Side, Signal, utc_now
from trading_agent.regimes import MIN_HISTORY, regime_series
from trading_agent.strategies import LIVE_STRATEGIES, STRATEGIES

H = 3_600_000


def make(n=700, seed=1, volume=True):
    """Marché synthétique en vagues : chaque stratégie du projet y émet des signaux."""
    rng = random.Random(seed)
    p, out = 100.0, []
    for i in range(n):
        p *= 1 + 0.004 * math.sin(i / 40) + 0.0015 + rng.gauss(0, 0.012)
        o = p * (1 + rng.gauss(0, 0.003))
        h = max(o, p) * (1 + abs(rng.gauss(0, 0.004)))
        l = min(o, p) * (1 - abs(rng.gauss(0, 0.004)))
        v = 100 * (6 if rng.random() < 0.03 else 1) * rng.uniform(0.5, 1.5)
        out.append([i * H, o, h, l, p, v] if volume else [i * H, o, h, l, p])
    return out


CANDLES = make()


@pytest.fixture(scope="module")
def project_report():
    return analyze_all(CANDLES, checkpoints=3, seeds=2)


# ---- les stratégies et régimes du projet : sains, et vraiment vérifiés ----

def test_every_project_strategy_is_verified_clean(project_report):
    for name in LIVE_STRATEGIES:   # les candidates « backtest seulement » ont leurs propres tests
        report = project_report["reports"][name]
        assert report["status"] == SAIN, (name, report)
        assert report["entry"]["signal_points"] >= 1, name
        assert report["exit"]["signal_points"] >= 1, name
    assert project_report["reports"]["regimes"]["status"] == SAIN


def test_regimes_are_verified_clean_on_long_history(project_report):
    report = project_report["reports"]["regimes"]
    assert report["status"] == SAIN and report["points_checked"] >= 1


def test_regimes_on_short_history_are_never_clean():
    short = make(MIN_HISTORY - 1)
    assert all(r is None for r in regime_series(short))
    report = analyze_series(regime_series, short)
    assert report["status"] == INSUFFISANT and report["points_checked"] == 0


# ---- fuites que l'analyse doit attraper ----

def centered_average(candles):
    """Moyenne centrée : utilise les 2 bougies suivantes (fuite classique)."""
    closes = [float(c[4]) for c in candles]
    return [sum(closes[max(0, i - 2): i + 3]) / len(closes[max(0, i - 2): i + 3]) for i in range(len(closes))]


def test_series_using_future_is_caught():
    report = analyze_series(centered_average, CANDLES)
    assert report["status"] == FUITE and report["details"]


def test_series_with_only_none_is_insufficient_not_clean():
    assert analyze_series(lambda c: [None] * len(c), CANDLES)["status"] == INSUFFISANT


def test_series_of_wrong_length_is_an_error():
    assert analyze_series(lambda c: [1] * (len(c) - 1), CANDLES)["status"] == ERREUR


def make_peeking_exit(offset=5):
    """Sortie qui garde en mémoire toutes les bougies vues et lit celle d'après :
    l'état caché persiste d'un appel à l'autre, hors de la fenêtre reçue."""
    seen = {}

    def exit_(history):
        for c in history:
            seen[c[0]] = float(c[4])
        future = seen.get(history[-1][0] + offset * H)
        return future is not None and future > float(history[-1][4])
    return exit_


def test_hidden_state_leak_is_caught_far_beyond_the_window():
    candles = make(1200)
    report = analyze_decision(make_peeking_exit(), candles, window=300, checkpoints=6, seeds=2)
    assert report["status"] == FUITE
    # le point fautif le plus ancien est à plus d'une fenêtre de la fin de l'historique
    assert min(d["index"] for d in report["details"]) < len(candles) - 300 - 1


def test_leak_on_confidence_only_is_caught():
    seen = {}

    def entry(history):
        for c in history:
            seen[c[0]] = float(c[4])
        last = float(history[-1][4])
        future = seen.get(history[-1][0] + H, last)
        return Signal(symbol="X", side=Side.BUY, confidence=0.9 if future > last else 0.6,
                      reference_price=last, stop_price=last * 0.9, rationale="r",
                      timestamp=utc_now(), strategy="t")

    report = analyze_decision(entry, make(500), checkpoints=4, seeds=2)
    assert report["status"] == FUITE


def test_unreproducible_decision_is_not_clean():
    calls = {"n": 0}

    def flaky(history):
        calls["n"] += 1
        return calls["n"] % 2 == 0

    assert analyze_decision(flaky, make(400), checkpoints=3, seeds=1)["status"] in (INSTABLE, FUITE)


def test_exception_is_an_error_not_clean():
    def boom(history):
        if len(history) > 100:
            raise RuntimeError("boom")
        return False
    report = analyze_decision(boom, make(400), checkpoints=2, seeds=1)
    assert report["status"] == ERREUR and "boom" in report["reason"]


def test_strategy_mutating_candles_is_an_error():
    def mutate(history):
        history[-1][5] = 0.0
        return True
    assert analyze_decision(mutate, make(400), checkpoints=2, seeds=1)["status"] == ERREUR


def test_no_signal_is_insufficient_and_can_be_relaxed():
    assert analyze_decision(lambda h: None, make(400), checkpoints=2, seeds=1)["status"] == INSUFFISANT
    relaxed = analyze_decision(lambda h: None, make(400), checkpoints=2, seeds=1, require_signal=False)
    assert relaxed["status"] == SAIN


def test_budget_and_invalid_parameters_are_refused():
    assert analyze_decision(lambda h: True, make(400), max_calls=100)["status"] == INSUFFISANT
    assert analyze_decision(lambda h: True, make(400), checkpoints=True)["status"] == ERREUR
    assert analyze_decision(lambda h: True, make(400), window=0)["status"] == ERREUR
    assert analyze_series(regime_series, make(400), seeds=2.0)["status"] == ERREUR
    assert analyze_decision(lambda h: True, [])["status"] == INSUFFISANT
    assert analyze_series(regime_series, None)["status"] == INSUFFISANT


def test_strategy_status_is_the_worst_of_entry_and_exit():
    leaky = STRATEGIES["breakout"].__class__(
        "leaky", "x", "x", STRATEGIES["breakout"].entry, make_peeking_exit(), "x", "x")
    report = analyze_strategy(leaky, make(500), checkpoints=3, seeds=2)
    assert report["entry"]["status"] == SAIN
    assert report["exit"]["status"] == FUITE and report["status"] == FUITE


# ---- futur inventé ----

def test_perturbed_future_keeps_past_and_shape():
    for candles in (make(50), make(50, volume=False), [tuple(c) for c in make(50)]):
        alt = perturb_future(candles, 20, seed=1)
        assert alt[:21] == candles[:21]
        assert len(alt) == len(candles)
        for a, b in zip(alt[21:], candles[21:]):
            assert type(a) is type(b) and len(a) == len(b) and a[0] == b[0]
            assert a[3] <= min(a[1], a[4]) <= max(a[1], a[4]) <= a[2]
        assert any(a[4] != b[4] for a, b in zip(alt[21:], candles[21:]))


def test_seeds_give_different_futures():
    candles = make(100)
    up, down = perturb_future(candles, 10, 0), perturb_future(candles, 10, 1)
    assert up[-1][4] != down[-1][4]


def test_strategy_without_exit_is_an_error_not_a_crash():
    class SansSortie:
        name = "sans_sortie"

        def entry(self, symbol, history):
            return None
    r = analyze_strategy(SansSortie(), [[i, 1.0, 1.0, 1.0, 1.0, 1.0] for i in range(10)])
    assert r["status"] == "erreur" and "sortie" in r["entry"]["reason"]
