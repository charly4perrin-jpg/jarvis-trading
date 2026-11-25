"""Analyse lookahead automatique : une décision prise à la bougie k dépend-elle du futur ?

Idée (reprise de l'« analyse lookahead » de freqtrade, réécrite ici sans rien
recopier, freqtrade étant sous GPL) : on calcule la décision à la bougie k une
première fois sur l'historique d'origine, puis on remplace toutes les bougies
postérieures à k par un futur inventé et on la recalcule. Une décision qui ne
regarde que le passé reste identique ; si elle change, elle lit le futur.

Pour les stratégies (entrée et sortie), la fonction est appelée comme dans le
backtest, sur la fenêtre des `window` bougies se terminant à k. Pour attraper un
état caché (cache, variable globale) rempli par le futur, la stratégie est
d'abord promenée sur TOUTES les fenêtres du futur inventé, puis sur la série
inventée entière, et seulement ensuite interrogée sur la fenêtre k.

Pour une fonction de série (une valeur par bougie, comme `regime_series`), on
compare la valeur à k sur la série d'origine, sur la série au futur inventé et
sur la série coupée à k.

Fail-closed : « sain » n'est rendu que si au moins un point a réellement été
comparé (et, pour une entrée, au moins un signal) et qu'aucun écart n'est vu.
Exception, sortie de forme inattendue, résultat non reproductible, bougies
modifiées par la stratégie ou budget de calcul dépassé : jamais « sain ».
L'analyse est un test de détection, pas une preuve : elle ne voit que ce que
les futurs inventés font bouger.
"""
import random
from dataclasses import asdict, is_dataclass

SAIN, FUITE, INSTABLE, INSUFFISANT, ERREUR = (
    "sain", "fuite", "non_reproductible", "insuffisant", "erreur")
DEFAULT_WINDOW, DEFAULT_CHECKPOINTS, DEFAULT_SEEDS = 300, 6, 3
MAX_CALLS = 200_000
_PRICE_COLS = (1, 2, 3, 4)


def _key(result):
    """Empreinte comparable d'un résultat. L'horodatage du signal (heure de calcul,
    pas de marché) est ignoré ; tout le reste, confiance comprise, est comparé."""
    if is_dataclass(result) and not isinstance(result, type):
        data = asdict(result)
        data.pop("timestamp", None)
        return ("dataclass", type(result).__name__, tuple(sorted((k, repr(v)) for k, v in data.items())))
    if isinstance(result, dict):
        return ("dict", tuple(sorted((k, repr(v)) for k, v in result.items())))
    return ("value", repr(result))


def _snapshot(candles) -> tuple:
    return tuple(tuple(c) for c in candles)


def perturb_future(candles: list, k: int, seed: int) -> list:
    """Copie de `candles` identique jusqu'à k inclus, avec un futur inventé après.

    Les bougies inventées gardent la forme exacte des originales (même longueur,
    même horodatage, même type de conteneur) : seuls prix et volume changent.
    La graine fixe le sens du futur (pair : hausse, impair : baisse)."""
    rng = random.Random(seed * 7919 + k)
    drift = 0.004 if seed % 2 == 0 else -0.004
    out = list(candles[:k + 1])
    factor = 1.0
    for c in candles[k + 1:]:
        factor *= max(0.5, min(1.5, 1 + drift + rng.gauss(0, 0.02)))
        factor = max(0.05, min(20.0, factor))
        new = list(c)
        prices = [float(c[i]) * factor * (1 + rng.uniform(-0.01, 0.01)) for i in _PRICE_COLS]
        o, h, l, cl = prices
        new[1], new[4] = o, cl
        new[2], new[3] = max(o, h, l, cl), min(o, h, l, cl)
        if len(c) > 5 and c[5] is not None:
            new[5] = float(c[5]) * rng.uniform(0.2, 5.0)
        out.append(type(c)(new) if isinstance(c, tuple) else new)
    return out


def _report(status, reason, checked=0, signals=0, details=None):
    return {"status": status, "reason": reason, "points_checked": checked,
            "signal_points": signals, "details": details or []}


def _pick(candidates: list[int], truthy: set[int], n: int) -> list[int]:
    """Jusqu'à n points : d'abord des points à signal, puis des points étalés."""
    if not candidates or n <= 0:
        return []
    with_signal = [i for i in candidates if i in truthy]
    step = max(1, len(with_signal) // max(1, (n + 1) // 2))
    chosen = with_signal[::step][:(n + 1) // 2]
    rest = [i for i in candidates if i not in chosen]
    if rest:
        spread = max(1, len(rest) // max(1, n - len(chosen)))
        chosen += rest[::spread][:n - len(chosen)]
    return sorted(set(chosen))


def analyze_decision(fn, candles: list, *, window=DEFAULT_WINDOW, checkpoints=DEFAULT_CHECKPOINTS,
                     seeds=DEFAULT_SEEDS, require_signal=True, max_calls=MAX_CALLS) -> dict:
    """Analyse une fonction `fn(history) -> décision` appelée bougie par bougie.

    `require_signal` : sans au moins un point vérifié où la décision est vraie
    (signal émis, sortie déclenchée), le statut est « insuffisant »."""
    if not isinstance(candles, list) or len(candles) < 3:
        return _report(INSUFFISANT, "historique trop court")
    if any(not isinstance(x, int) or isinstance(x, bool) or x < 1 for x in (window, checkpoints, seeds)):
        return _report(ERREUR, "paramètres invalides (entiers ≥ 1 attendus)")
    n = len(candles)
    cost = n + checkpoints * (1 + seeds * (n + 1))
    if cost > max_calls:
        return _report(INSUFFISANT, f"budget dépassé ({cost} appels > {max_calls}) : raccourcir l'historique")
    before = _snapshot(candles)

    def at(series, i):
        return series[max(0, i - window + 1): i + 1]

    try:
        base = {}
        for i in range(n):  # balayage chronologique, comme le backtest
            base[i] = _key(fn(at(candles, i)))
        truthy = {i for i in range(n) if base[i] not in (_key(None), _key(False))}
        points = _pick(list(range(n - 1)), truthy, checkpoints)  # il faut un futur à inventer
        details, unstable, signals = [], [], 0
        for k in points:
            if _key(fn(at(candles, k))) != base[k]:
                unstable.append({"index": k, "seed": None})
            for seed in range(seeds):
                alt = perturb_future(candles, k, seed)
                for i in range(k + 1, n):  # le futur inventé passe d'abord
                    fn(at(alt, i))
                fn(alt)
                if _key(fn(at(alt, k))) != base[k]:
                    details.append({"index": k, "seed": seed})
            signals += k in truthy
        if _snapshot(candles) != before:
            return _report(ERREUR, "la fonction a modifié les bougies reçues", len(points), signals)
    except Exception as exc:  # fail-closed : une erreur n'est jamais « sain »
        return _report(ERREUR, f"{type(exc).__name__}: {exc}")
    if details:
        return _report(FUITE, f"décision modifiée par le futur sur {len({d['index'] for d in details})} point(s)",
                       len(points), signals, details)
    if unstable:
        return _report(INSTABLE, "décision différente au second appel sur le même historique",
                       len(points), signals, unstable)
    if not points:
        return _report(INSUFFISANT, "aucun point vérifiable", 0, 0)
    if require_signal and signals == 0:
        return _report(INSUFFISANT, "aucun signal sur cet historique : rien de probant à vérifier",
                       len(points), 0)
    return _report(SAIN, "aucune dépendance au futur détectée", len(points), signals)


def analyze_series(fn, candles: list, *, checkpoints=DEFAULT_CHECKPOINTS, seeds=DEFAULT_SEEDS) -> dict:
    """Analyse une fonction de série `fn(candles) -> liste` (une valeur par bougie).

    Seuls les points où la valeur d'origine n'est pas None sont vérifiables ;
    s'il n'y en a aucun, le statut est « insuffisant », jamais « sain »."""
    if not isinstance(candles, list) or len(candles) < 3:
        return _report(INSUFFISANT, "historique trop court")
    if any(not isinstance(x, int) or isinstance(x, bool) or x < 1 for x in (checkpoints, seeds)):
        return _report(ERREUR, "paramètres invalides (entiers ≥ 1 attendus)")
    n = len(candles)
    before = _snapshot(candles)

    def run(series):
        out = fn(series)
        if not isinstance(out, list) or len(out) != len(series):
            raise ValueError("la fonction doit rendre une liste de même longueur que les bougies")
        return out

    try:
        base = run(candles)
        valid = [i for i in range(n - 1) if base[i] is not None]
        points = _pick(valid, set(), checkpoints)
        details = []
        for k in points:
            ref = _key(base[k])
            if _key(run(candles[:k + 1])[k]) != ref:
                details.append({"index": k, "seed": None})
            for seed in range(seeds):
                if _key(run(perturb_future(candles, k, seed))[k]) != ref:
                    details.append({"index": k, "seed": seed})
        if _key(run(candles)) != _key(base):  # comparaison complète, pas seulement aux points
            return _report(INSTABLE, "série différente au second calcul", len(points))
        if _snapshot(candles) != before:
            return _report(ERREUR, "la fonction a modifié les bougies reçues", len(points))
    except Exception as exc:
        return _report(ERREUR, f"{type(exc).__name__}: {exc}")
    if details:
        return _report(FUITE, f"valeur modifiée par le futur sur {len({d['index'] for d in details})} point(s)",
                       len(points), 0, details)
    if not points:
        return _report(INSUFFISANT, "aucune valeur calculée sur cet historique : rien de vérifié")
    return _report(SAIN, "aucune dépendance au futur détectée", len(points))


def analyze_strategy(strategy, candles: list, symbol="TEST/EUR", **kwargs) -> dict:
    """Entrée et sortie d'une stratégie de `strategies.STRATEGIES`. Sain seulement si
    les deux le sont ; la sortie exige au moins un déclenchement vérifié."""
    name = getattr(strategy, "name", repr(strategy))
    if not callable(getattr(strategy, "entry", None)) or not callable(getattr(strategy, "exit", None)):
        missing = _report(ERREUR, "stratégie sans fonction d'entrée ou de sortie")
        return {"strategy": name, "status": ERREUR, "entry": missing, "exit": missing}
    entry = analyze_decision(lambda h: strategy.entry(symbol, h), candles, **kwargs)
    exit_ = analyze_decision(strategy.exit, candles, **kwargs)
    worst = _worst([entry["status"], exit_["status"]])
    return {"strategy": name, "status": worst, "entry": entry, "exit": exit_}


_SEVERITY = [SAIN, INSUFFISANT, INSTABLE, ERREUR, FUITE]


def _worst(statuses: list[str]) -> str:
    if not statuses:
        return INSUFFISANT
    return max(statuses, key=lambda s: _SEVERITY.index(s) if s in _SEVERITY else len(_SEVERITY))


def analyze_all(candles: list, **kwargs) -> dict:
    """Toutes les stratégies du projet + le calcul des régimes, en lecture seule."""
    from trading_agent.regimes import regime_series
    from trading_agent.strategies import STRATEGIES

    reports = {name: analyze_strategy(s, candles, **kwargs) for name, s in STRATEGIES.items()}
    series_kwargs = {k: v for k, v in kwargs.items() if k in ("checkpoints", "seeds")}
    reports["regimes"] = analyze_series(regime_series, candles, **series_kwargs)
    return {"status": _worst([r["status"] for r in reports.values()]), "reports": reports}
