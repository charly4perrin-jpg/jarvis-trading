"""Régime de volatilité par modèle de Markov caché (HMM gaussien, Python pur).

On suppose que les rendements logarithmiques des bougies closes sont tirés par
un petit nombre d'états cachés (2 : calme / agité, ou 3 : calme / normal /
agité), chacun avec sa propre loi normale, et que le marché passe d'un état à
l'autre selon une matrice de transition. Le modèle est ajusté par
Baum-Welch (EM) sur les `window` dernières bougies uniquement, puis on lit la
probabilité *filtrée* (algorithme forward, sans lissage arrière) de chaque état
sur la dernière bougie : aucune donnée future n'entre dans la lecture.

Fail-closed :
- plus de MAX_FLAT_SHARE de clôtures inchangées (paire peu liquide) renvoie None ;
- toute entrée douteuse (paramètre non entier, booléen, hors bornes ; bougie
  qui n'est pas une liste/tuple ; prix non fini ou négatif ; calcul non fini)
  renvoie None ;
- la lecture n'est marquée « confiante » que si les états sont réellement
  distincts : le HMM doit battre une loi normale unique au critère BIC, les
  écarts-types de deux états voisins doivent différer d'au moins
  MIN_SEPARATION fois, chaque état doit occuper au moins MIN_OCCUPANCY des
  bougies, chaque état doit être persistant (probabilité de rester dans
  l'état d'une bougie à l'autre ≥ MIN_PERSISTENCE, soit une durée moyenne
  d'au moins 10 bougies), et la probabilité de l'état lu doit dépasser
  MIN_PROB sur chacune des CONFIRM_BARS dernières bougies. Sinon
  l'étiquette est « indéterminé » et confident=False (toujours les deux ensemble) : sur un marché homogène
  l'étiquette calme/agité serait arbitraire. La persistance écarte le cas
  des rendements indépendants à queues épaisses (Student-t) : un HMM y
  trouve deux « états » qui ne sont que le cœur et les queues d'une même loi,
  avec trans[i][i] ≈ occupation de l'état ; la confirmation sur plusieurs
  bougies empêche une seule grosse bougie de suffire à lire « agité ».

Coût : O(MAX_ITER × window × n_states²), borné par MAX_WINDOW et MAX_ITER.

Inspiration : formulation classique de Rabiner (1989), et l'enveloppe
GaussianHMM de sktime (BSD-3-Clause, sktime/detection/hmm_learn/gaussian.py)
pour l'interface ; le code ici est écrit à neuf, sans dépendance.
"""
import math
from numbers import Real

# 250 variations = 251 bougies : tient dans les 300 bougies chargées par les agents.
DEFAULT_WINDOW, MIN_WINDOW, MAX_WINDOW = 250, 100, 1000
MAX_ITER, TOL = 60, 1e-6
MIN_SEPARATION = 1.5
MIN_OCCUPANCY = 0.1
MIN_PROB = 0.8
MIN_PERSISTENCE = 0.9
CONFIRM_BARS = 3
MAX_REFIT_EVERY = 30   # hmm_series : au-delà, les paramètres seraient trop périmés
MAX_FLAT_SHARE = 0.2   # au-delà, trop de clôtures inchangées (paire peu liquide) : pas de lecture
LABELS = {2: ["calme", "agité"], 3: ["calme", "normal", "agité"]}
_LOG_2PI = math.log(2 * math.pi)


def _strict_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _close_of(c) -> float | None:
    """Clôture valide (finie, > 0) d'une bougie, ou None."""
    if not isinstance(c, (list, tuple)) or len(c) < 5:
        return None
    v = c[4]
    if isinstance(v, bool) or not isinstance(v, Real):
        return None
    v = float(v)
    return v if math.isfinite(v) and v > 0 else None


def _closes(candles, window: int) -> list[float] | None:
    if not isinstance(candles, (list, tuple)) or len(candles) < window + 1:
        return None
    closes = []
    for c in candles[-(window + 1):]:
        v = _close_of(c)
        if v is None:
            return None
        closes.append(v)
    return closes


def _log_emissions(x: list[float], means: list[float], variances: list[float]):
    """Log-densités normalisées par ligne (soustraction du max) + somme des max."""
    rows, offset = [], 0.0
    for v in x:
        logs = [-0.5 * (_LOG_2PI + math.log(s2) + (v - m) ** 2 / s2) for m, s2 in zip(means, variances)]
        top = max(logs)
        offset += top
        rows.append([math.exp(lg - top) for lg in logs])
    return rows, offset


def _forward(b, pi, trans):
    k = len(pi)
    alphas, loglik = [], 0.0
    prev = None
    for t, row in enumerate(b):
        if t == 0:
            a = [pi[j] * row[j] for j in range(k)]
        else:
            a = [sum(prev[i] * trans[i][j] for i in range(k)) * row[j] for j in range(k)]
        c = sum(a)
        if not (c > 0 and math.isfinite(c)):
            raise ArithmeticError("forward dégénéré")
        a = [v / c for v in a]
        loglik += math.log(c)
        alphas.append(a)
        prev = a
    return alphas, loglik


def _e_step(x, pi, means, variances, trans):
    """Forward-backward mis à l'échelle : émissions, alphas filtrés, betas,
    gammas lissés et log-vraisemblance."""
    k, n = len(pi), len(x)
    b, offset = _log_emissions(x, means, variances)
    alphas, ll = _forward(b, pi, trans)
    betas = [[1.0] * k for _ in range(n)]
    for t in range(n - 2, -1, -1):
        nxt = [betas[t + 1][j] * b[t + 1][j] for j in range(k)]
        row = [sum(trans[i][j] * nxt[j] for j in range(k)) for i in range(k)]
        s = sum(row)
        if not (s > 0 and math.isfinite(s)):
            raise ArithmeticError("backward dégénéré")
        betas[t] = [v / s for v in row]
    gammas = []
    for a, be in zip(alphas, betas):
        g = [ai * bi for ai, bi in zip(a, be)]
        s = sum(g)
        if not (s > 0 and math.isfinite(s)):
            raise ArithmeticError("gamma dégénéré")
        gammas.append([v / s for v in g])
    return b, alphas, betas, gammas, ll + offset


def _fit(x: list[float], k: int):
    n = len(x)
    mean = sum(x) / n
    var = sum((v - mean) ** 2 for v in x) / n
    if not (var > 0 and math.isfinite(var)):
        return None
    floor = var * 1e-3
    # Initialisation déterministe : variances des k tranches de |écart| triés.
    dev = sorted((v - mean) ** 2 for v in x)
    size = n // k
    variances = [max(sum(dev[i * size:(i + 1) * size]) / size, floor) for i in range(k)]
    means = [mean] * k
    pi = [1.0 / k] * k
    trans = [[0.9 if i == j else 0.1 / (k - 1) for j in range(k)] for i in range(k)]
    prev_ll = -math.inf
    for _ in range(MAX_ITER):
        b, alphas, betas, gammas, ll = _e_step(x, pi, means, variances, trans)
        xi = [[0.0] * k for _ in range(k)]
        for t in range(n - 1):
            m = [[alphas[t][i] * trans[i][j] * b[t + 1][j] * betas[t + 1][j] for j in range(k)] for i in range(k)]
            s = sum(map(sum, m))
            for i in range(k):
                for j in range(k):
                    xi[i][j] += m[i][j] / s
        pi = gammas[0][:]
        for i in range(k):
            row_sum = sum(xi[i])
            trans[i] = [max(v / row_sum, 1e-6) for v in xi[i]] if row_sum > 0 else [1.0 / k] * k
            tot = sum(trans[i])
            trans[i] = [v / tot for v in trans[i]]
            w = sum(g[i] for g in gammas)
            if w <= 1e-9:
                return None
            means[i] = sum(g[i] * v for g, v in zip(gammas, x)) / w
            variances[i] = max(sum(g[i] * (v - means[i]) ** 2 for g, v in zip(gammas, x)) / w, floor)
        if not math.isfinite(ll):
            return None
        if abs(ll - prev_ll) < TOL * max(1.0, abs(ll)):
            break
        prev_ll = ll
    # Lecture finale cohérente : alphas, gammas (occupation) et vraisemblance
    # viennent tous des paramètres finaux.
    _, alphas, _, gammas, ll = _e_step(x, pi, means, variances, trans)
    return {"means": means, "variances": variances, "trans": trans, "alphas": alphas,
            "gammas": gammas, "loglik": ll, "var1": var, "floor": floor}


def hmm_regime(candles, window=DEFAULT_WINDOW, n_states=2) -> dict | None:
    """Régime caché de la dernière bougie close, ou None si la lecture est
    impossible. Seules les `window` dernières variations sont utilisées."""
    if not _strict_int(window) or not MIN_WINDOW <= window <= MAX_WINDOW:
        return None
    if not _strict_int(n_states) or n_states not in LABELS:
        return None
    closes = _closes(candles, window)
    if closes is None:
        return None
    x = [math.log(b / a) for a, b in zip(closes, closes[1:])]
    if sum(1 for v in x if v == 0.0) > MAX_FLAT_SHARE * len(x):
        return None
    model = _model(x, n_states)
    return None if model is None else _reading(model, model["tail"])


def _model(x: list[float], k: int) -> dict | None:
    """Ajuste le HMM sur x et fige ce qu'il faut pour lire les bougies suivantes :
    paramètres, ordre des états, diagnostics et les CONFIRM_BARS derniers alphas."""
    try:
        fit = _fit(x, k)
    except (ArithmeticError, ValueError, ZeroDivisionError):
        return None
    if fit is None:
        return None
    n = len(x)
    order = sorted(range(k), key=lambda i: fit["variances"][i])
    sigmas = [math.sqrt(fit["variances"][i]) for i in order]
    occupancy = [0] * k
    for g in fit["gammas"]:
        occupancy[order.index(max(range(k), key=g.__getitem__))] += 1
    occupancy = [c / n for c in occupancy]
    # BIC : HMM à k états contre une loi normale unique (2 paramètres).
    ll1 = -0.5 * n * (_LOG_2PI + math.log(fit["var1"]) + 1)
    params = k * (k - 1) + (k - 1) + 2 * k
    bic_gain = (fit["loglik"] - ll1) - 0.5 * (params - 2) * math.log(n)
    separation = min(sigmas[i + 1] / sigmas[i] for i in range(k - 1))
    persistence = [fit["trans"][i][i] for i in order]
    values = sigmas + persistence + [bic_gain, separation]
    if not all(math.isfinite(v) for v in values):
        return None
    distinct = (bic_gain > 0 and separation >= MIN_SEPARATION
                and min(occupancy) >= MIN_OCCUPANCY and min(persistence) >= MIN_PERSISTENCE
                # un état collé au plancher de variance n'est pas un régime, c'est un artefact
                and min(fit["variances"]) > 1.5 * fit["floor"])
    return {"k": k, "order": order, "sigmas": sigmas, "persistence": persistence,
            "separation": separation, "occupancy": occupancy, "bic_gain": bic_gain, "n_obs": n,
            "distinct": distinct, "means": fit["means"], "variances": fit["variances"],
            "trans": fit["trans"], "tail": [a[:] for a in fit["alphas"][-CONFIRM_BARS:]]}


def _reading(model: dict, tail: list[list[float]]) -> dict | None:
    """Lecture de la dernière bougie à partir des CONFIRM_BARS derniers alphas filtrés."""
    k, order = model["k"], model["order"]
    last = [tail[-1][i] for i in order]
    if not all(math.isfinite(v) for v in last):
        return None
    sigmas, persistence = model["sigmas"], model["persistence"]
    separation, occupancy, bic_gain = model["separation"], model["occupancy"], model["bic_gain"]
    n = model["n_obs"]
    state = max(range(k), key=last.__getitem__)
    # Confirmation : l'état lu domine (≥ MIN_PROB) sur les CONFIRM_BARS
    # dernières bougies, pas seulement sur la dernière.
    confirmed = len(tail) >= CONFIRM_BARS and all(
        tail[-1 - t][order[state]] >= MIN_PROB for t in range(CONFIRM_BARS))
    confident = model["distinct"] and confirmed
    return {
        # Jamais d'étiquette sans confiance : un faux « calme » pourrait autoriser des entrées.
        "state": LABELS[k][state] if confident else "indéterminé",
        "confident": confident,
        "prob": round(last[state], 4),
        "probs": {LABELS[k][i]: round(p, 4) for i, p in enumerate(last)},
        "sigmas": [round(s, 6) for s in sigmas],
        "persistence": [round(p, 4) for p in persistence],
        "separation": round(separation, 3),
        "occupancy": [round(o, 3) for o in occupancy],
        "bic_gain": round(bic_gain, 2),
        "n_obs": n,
    }


def hmm_series(candles, window=DEFAULT_WINDOW, n_states=2, refit_every=7) -> list:
    """Lecture de Markov de chaque bougie, causale et rapide (pour le backtest).

    Le modèle est réajusté sur les `window` variations passées seulement aux
    bougies window, window + refit_every, ... (calendrier fixé depuis le début
    de la série : ajouter des bougies futures ne déplace aucun réajustement).
    À ces bougies, la lecture est exactement celle de hmm_regime. Entre deux,
    chaque bougie est lue par une étape du filtre forward avec les derniers
    paramètres ajustés : aucune donnée future, mêmes règles de confiance.

    Fail-closed : paramètre invalide, bougie invalide ou trop de clôtures
    inchangées dans la fenêtre donnent None ; une bougie invalide abandonne le
    modèle jusqu'au prochain réajustement réussi."""
    if not isinstance(candles, (list, tuple)):
        return []
    n = len(candles)
    out = [None] * n
    if (not _strict_int(window) or not MIN_WINDOW <= window <= MAX_WINDOW
            or not _strict_int(n_states) or n_states not in LABELS
            or not _strict_int(refit_every) or not 1 <= refit_every <= MAX_REFIT_EVERY):
        return out
    closes = [_close_of(c) for c in candles]
    # x[i] : variation de la bougie i-1 à la bougie i (None si l'une est invalide).
    x = [None] + [math.log(b / a) if a is not None and b is not None else None
                  for a, b in zip(closes, closes[1:])]
    bad = flat = 0
    model = tail = None
    for i in range(window, n):
        if i == window:
            seen = x[1:window + 1]
            bad = sum(1 for v in seen if v is None)
            flat = sum(1 for v in seen if v == 0.0)
        else:
            new, old = x[i], x[i - window]
            bad += (new is None) - (old is None)
            flat += (new == 0.0) - (old == 0.0)
        usable = bad == 0 and flat <= MAX_FLAT_SHARE * window
        if (i - window) % refit_every == 0:
            model = _model(x[i - window + 1:i + 1], n_states) if usable else None
            tail = model["tail"] if model else None
        elif model is not None:
            tail = _step(model, tail, x[i])
            if tail is None:
                model = None
        if model is not None and usable:
            out[i] = _reading(model, tail)
    return out


def _step(model: dict, tail: list[list[float]], v) -> list[list[float]] | None:
    """Une étape du filtre forward (mêmes calculs que _forward) ; None si dégénérée."""
    if v is None:
        return None
    b = _log_emissions([v], model["means"], model["variances"])[0][0]
    prev, trans, k = tail[-1], model["trans"], model["k"]
    a = [sum(prev[i] * trans[i][j] for i in range(k)) * b[j] for j in range(k)]
    c = sum(a)
    if not (c > 0 and math.isfinite(c)):
        return None
    return (tail + [[p / c for p in a]])[-CONFIRM_BARS:]


def hmm_blocks(strategy, hmm) -> str | None:
    """Même veto d'entrée que l'essai en direct (StrategyAnalyst) : une phase
    agitée confirmée écarte une stratégie non prévue pour la forte volatilité.
    Renvoie la raison du refus, ou None si l'entrée n'est pas bloquée."""
    if (isinstance(hmm, dict) and hmm.get("confident") and hmm.get("state") == "agité"
            and not getattr(strategy, "high_vol", False)):
        return "modèle de Markov : phase agitée confirmée"
    return None
