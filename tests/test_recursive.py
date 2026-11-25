from types import SimpleNamespace

from tests.test_lookahead import make
from trading_agent.recursive import (
    ERREUR, INSTABLE, INSUFFISANT, STABLE, analyze_all, analyze_strategy)
from trading_agent.strategies import STRATEGIES, ema, sma

CANDLES = make(1100)


def closes(h):
    return [float(c[4]) for c in h]


def strat(entry, exit_=None, name="t"):
    return SimpleNamespace(name=name, entry=entry,
                           exit=exit_ or (lambda h: float(h[-1][4]) < sma(closes(h), 20)))


# La valeur de la moyenne sert de « signal » : le moindre écart de calcul se voit.
SMA50 = strat(lambda s, h: {"sma": round(sma(closes(h), 50), 9)})
EMA200 = strat(lambda s, h: float(h[-1][4]) > ema(closes(h), 200),
               lambda h: float(h[-1][4]) < ema(closes(h), 200))


def test_pure_sma_is_stable_from_its_period():
    r = analyze_strategy(SMA50, CANDLES, lengths=(20, 50, 100, 200))
    assert r["status"] == STABLE and r["min_stable"] == 50 and r["reference"] == 200
    assert r["lengths"][20]["disagreement"] > 0 and r["lengths"][50]["disagreement"] == 0
    assert r["signal_points"] > 0


def test_long_ema_is_unstable_on_short_history():
    r = analyze_strategy(EMA200, CANDLES, lengths=(200, 300, 1000), tail=200)
    assert r["status"] == INSTABLE
    assert r["lengths"][200]["disagreement"] > 0
    assert r["min_stable"] is None or r["min_stable"] > 200


def test_strategy_without_entry_is_error():
    r = analyze_strategy(SimpleNamespace(name="x", exit=lambda h: False), CANDLES)
    assert r["status"] == ERREUR


def test_crashing_strategy_is_error():
    def boom(s, h):
        raise RuntimeError("kaput")
    r = analyze_strategy(strat(boom), CANDLES, lengths=(50, 100))
    assert r["status"] == ERREUR and "kaput" in r["reason"]


def test_mutating_strategy_is_error():
    def mutate(s, h):
        h[-1][4] = 0.0
        return False
    data = make(300)
    r = analyze_strategy(strat(mutate), data, lengths=(50, 100))
    assert r["status"] == ERREUR


def test_no_signal_is_never_stable():
    r = analyze_strategy(strat(lambda s, h: None, lambda h: False), CANDLES, lengths=(50, 100))
    assert r["status"] == INSUFFISANT and r["lengths"][50]["disagreement"] == 0


def test_short_history_drops_lengths_and_needs_two():
    short = make(260)
    r = analyze_strategy(SMA50, short, lengths=(50, 200, 1000), tail=50)
    assert r["reference"] == 200 and 1000 not in r["lengths"]
    r = analyze_strategy(SMA50, make(120), lengths=(200, 300))
    assert r["status"] == INSUFFISANT


def test_stable_beyond_backtest_window_is_unstable():
    r = analyze_strategy(SMA50, CANDLES, lengths=(20, 50, 100, 200), window=40)
    assert r["status"] == INSTABLE and r["min_stable"] == 50


def test_invalid_parameters_and_budget():
    assert analyze_strategy(SMA50, CANDLES, lengths=("a",))["status"] == ERREUR
    assert analyze_strategy(SMA50, CANDLES, tail=0)["status"] == ERREUR
    assert analyze_strategy(SMA50, CANDLES, lengths=(50, 100), max_calls=10)["status"] == INSUFFISANT
    assert analyze_strategy(SMA50, "pas une liste")["status"] == INSUFFISANT


def test_project_strategies_all_reported():
    out = analyze_all(CANDLES, tail=20)
    assert set(out["reports"]) == set(STRATEGIES)
    for name, r in out["reports"].items():
        assert r["status"] in (STABLE, INSTABLE, INSUFFISANT), (name, r)
        assert r["reference"] == 1000
