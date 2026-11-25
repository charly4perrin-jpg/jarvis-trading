"""Analyse de récursivité : les signaux dépendent-ils de la longueur d'historique ?

Idée (reprise de l'« analyse récursive » de freqtrade, réécrite ici sans rien
recopier, freqtrade étant sous GPL) : une EMA démarre sur la première bougie
reçue, donc sa valeur, et le signal qui en découle, change selon qu'on lui
donne 200 ou 1000 bougies. Le backtest donne `window` = 300 bougies aux
stratégies ; le direct peut en donner un autre nombre.

Pour chaque longueur L, on recalcule entrée et sortie sur les mêmes `tail`
dernières bougies, chaque fois avec exactement les L bougies qui précèdent
(bougie comprise). On compare au résultat obtenu avec la plus longue longueur
disponible (la référence) et on mesure le taux de désaccord.

`min_stable` est la plus petite longueur à partir de laquelle toutes les
longueurs testées, jusqu'à la référence, donnent exactement les mêmes décisions.
Fail-closed : « stable » n'est rendu que si au moins deux longueurs ont été
comparées, qu'au moins un signal (entrée ou sortie) a été vu sur la référence,
qu'une longueur plus courte que la référence la rejoint, et qu'elle tient dans
la fenêtre du backtest. Toute exception ou bougie modifiée : « erreur ».
"""
from trading_agent.lookahead import _key, _snapshot

STABLE, INSTABLE, INSUFFISANT, ERREUR = "stable", "instable", "insuffisant", "erreur"
DEFAULT_LENGTHS = (200, 300, 500, 1000)
DEFAULT_TAIL, BACKTEST_WINDOW = 50, 300
MAX_CALLS = 20_000


def _report(status, reason, **extra):
    out = {"status": status, "reason": reason, "min_stable": None, "reference": None,
           "lengths": {}, "signal_points": 0}
    out.update(extra)
    return out


def analyze_strategy(strategy, candles: list, *, lengths=DEFAULT_LENGTHS, tail=DEFAULT_TAIL,
                     window=BACKTEST_WINDOW, symbol="TEST/EUR", max_calls=MAX_CALLS) -> dict:
    """Stabilité des décisions d'une stratégie selon l'historique disponible."""
    name = getattr(strategy, "name", repr(strategy))
    entry, exit_ = getattr(strategy, "entry", None), getattr(strategy, "exit", None)
    if not callable(entry) or not callable(exit_):
        return {"strategy": name, **_report(ERREUR, "stratégie sans fonction d'entrée ou de sortie")}
    if not isinstance(candles, list):
        return {"strategy": name, **_report(INSUFFISANT, "bougies absentes")}
    try:
        lengths = sorted({int(x) for x in lengths})
    except (TypeError, ValueError):
        return {"strategy": name, **_report(ERREUR, "longueurs invalides")}
    if (not lengths or lengths[0] < 1 or any(not isinstance(x, int) or isinstance(x, bool) or x < 1
                                              for x in (tail, window))):
        return {"strategy": name, **_report(ERREUR, "paramètres invalides (entiers ≥ 1 attendus)")}
    usable = [L for L in lengths if L + tail - 1 <= len(candles)]
    if len(usable) < 2:
        return {"strategy": name, **_report(
            INSUFFISANT, f"historique trop court ({len(candles)} bougies) pour comparer deux longueurs")}
    cost = 2 * tail * len(usable)
    if cost > max_calls:
        return {"strategy": name, **_report(INSUFFISANT, f"budget dépassé ({cost} appels > {max_calls})")}
    ref = usable[-1]
    n = len(candles)
    targets = range(n - tail, n)
    before = _snapshot(candles)
    try:
        decisions = {}
        for L in usable:
            decisions[L] = [(_key(entry(symbol, candles[i - L + 1:i + 1])), _key(exit_(candles[i - L + 1:i + 1])))
                            for i in targets]
        if _snapshot(candles) != before:
            return {"strategy": name, **_report(ERREUR, "la stratégie a modifié les bougies reçues")}
    except Exception as exc:  # fail-closed : une stratégie qui plante n'est jamais « stable »
        return {"strategy": name, **_report(ERREUR, f"{type(exc).__name__}: {exc}")}

    falsy = (_key(None), _key(False))
    signals = sum((e not in falsy) + (x not in falsy) for e, x in decisions[ref])
    per_length = {}
    for L in usable:
        e_bad = sum(a[0] != b[0] for a, b in zip(decisions[L], decisions[ref]))
        x_bad = sum(a[1] != b[1] for a, b in zip(decisions[L], decisions[ref]))
        per_length[L] = {"entry_disagreement": e_bad / tail, "exit_disagreement": x_bad / tail,
                         "disagreement": (e_bad + x_bad) / (2 * tail)}
    min_stable = None
    for L in reversed(usable[:-1]):  # du plus long au plus court, tant que tout concorde
        if per_length[L]["disagreement"] > 0:
            break
        min_stable = L
    extra = {"min_stable": min_stable, "reference": ref, "lengths": per_length, "signal_points": signals}
    if signals == 0:
        return {"strategy": name, **_report(
            INSUFFISANT, "aucun signal sur les dernières bougies : rien de probant à comparer", **extra)}
    if min_stable is None:
        return {"strategy": name, **_report(
            INSTABLE, f"aucune longueur plus courte que {ref} bougies ne donne les mêmes signaux", **extra)}
    if min_stable > window:
        return {"strategy": name, **_report(
            INSTABLE, f"stable à partir de {min_stable} bougies seulement, le backtest en donne {window}", **extra)}
    return {"strategy": name, **_report(STABLE, f"signaux identiques dès {min_stable} bougies", **extra)}


_SEVERITY = [STABLE, INSUFFISANT, INSTABLE, ERREUR]


def _worst(statuses: list[str]) -> str:
    if not statuses:
        return INSUFFISANT
    return max(statuses, key=lambda s: _SEVERITY.index(s) if s in _SEVERITY else len(_SEVERITY))


def analyze_all(candles: list, **kwargs) -> dict:
    """Toutes les stratégies du projet, en lecture seule."""
    from trading_agent.strategies import STRATEGIES

    reports = {name: analyze_strategy(s, candles, **kwargs) for name, s in STRATEGIES.items()}
    return {"status": _worst([r["status"] for r in reports.values()]), "reports": reports}
