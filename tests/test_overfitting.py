"""Sharpe dégonflé et PBO : détecter le hasard, reconnaître un vrai avantage."""
import random

from trading_agent.overfitting import deflated_sharpe, expected_max_sharpe, moments, pbo, sharpe_per_period


def noise(seed, n=1000, drift=0.0):
    rng = random.Random(seed)
    return [rng.gauss(drift, 0.01) for _ in range(n)]


def test_best_of_many_random_strategies_is_recognised_as_luck():
    trials = {f"s{i}": noise(i) for i in range(30)}
    sharpes = [sharpe_per_period(r) for r in trials.values()]
    best = max(trials, key=lambda k: sharpe_per_period(trials[k]))
    assert sharpe_per_period(trials[best]) > 0                 # le meilleur « paraît » bon…
    assert deflated_sharpe(trials[best], sharpes)["dsr"] < 0.95  # …mais ne résiste pas au nombre d'essais
    assert pbo(trials)["pbo"] > 0.3


def test_a_real_edge_survives_both_tests():
    trials = {f"s{i}": noise(i) for i in range(20)}
    trials["vrai"] = noise(99, drift=0.0015)                   # Sharpe par période d'environ 0,15
    sharpes = [sharpe_per_period(r) for r in trials.values()]
    assert deflated_sharpe(trials["vrai"], sharpes)["dsr"] > 0.95
    result = pbo(trials)
    assert result["pbo"] < 0.2 and result["most_chosen"] == "vrai"


def test_more_trials_means_more_doubt():
    r = noise(5, drift=0.0006)
    sharpes = [sharpe_per_period(noise(i)) for i in range(10)] + [sharpe_per_period(r)]
    few = deflated_sharpe(r, sharpes, n_trials=3)["dsr"]
    many = deflated_sharpe(r, sharpes, n_trials=300)["dsr"]
    assert few > many


def test_helpers_edge_cases():
    assert expected_max_sharpe([0.1]) == 0.0
    skew, kurt = moments(noise(1, n=20000))
    assert abs(skew) < 0.1 and abs(kurt - 3) < 0.15
    assert pbo({"a": [0.0] * 5, "b": [0.0] * 5})["pbo"] is None
