"""Régimes de marché : dans quel type de marché sommes-nous, et faut-il trader ?

Chaque bougie close reçoit une étiquette calculée uniquement avec le passé
(aucune donnée future), selon des critères chiffrés :

- tendance : haussière si le prix est au-dessus de sa moyenne 200 bougies, que la
  moyenne 50 est au-dessus de la 200 et que la 200 monte depuis 20 bougies ;
  baissière dans le cas inverse ; sinon « sans tendance » ;
- volatilité : écart-type des variations des 20 dernières bougies comparé à celui
  des 200 dernières ; haute au-delà de 1,5 fois, basse en dessous de 0,67 fois ;
- mouvement exceptionnel : la dernière bougie a bougé de plus de 4 écarts-types ;
- volume anormalement faible : moins de 20 % du volume moyen des 100 dernières.

Le filtre est volontairement fixé à l'avance, d'après la logique de chaque
stratégie, et non ajusté sur les résultats : ajuster les régimes autorisés sur
l'historique reviendrait à sur-optimiser.
"""
import math

TREND_FAST, TREND_SLOW, SLOPE = 50, 200, 20
VOL_SHORT, VOL_LONG = 20, 200
HIGH_VOL, LOW_VOL = 1.5, 0.67
SHOCK_SIGMA = 4.0
THIN_VOLUME, VOLUME_WINDOW = 0.2, 100
MIN_HISTORY = TREND_SLOW + SLOPE

TRENDS = ["haussier", "sans tendance", "baissier"]
VOLATILITIES = ["basse", "normale", "haute"]


def _prefix(values: list[float]) -> list[float]:
    out = [0.0]
    for v in values:
        out.append(out[-1] + v)
    return out


def regime_series(candles: list) -> list[dict | None]:
    """Régime de chaque bougie (None tant que l'historique est trop court)."""
    closes = [float(c[4]) for c in candles]
    volumes = [float(c[5]) if len(c) > 5 and c[5] is not None else 0.0 for c in candles]
    rets = [0.0] + [math.log(b / a) if a > 0 and b > 0 else 0.0 for a, b in zip(closes, closes[1:])]
    pc, pr, pr2, pv = _prefix(closes), _prefix(rets), _prefix([r * r for r in rets]), _prefix(volumes)

    def mean(p, i, n):  # moyenne des n valeurs se terminant à l'indice i inclus
        return (p[i + 1] - p[i + 1 - n]) / n

    def std(i, n):
        m = mean(pr, i, n)
        return math.sqrt(max(mean(pr2, i, n) - m * m, 0.0))

    out: list[dict | None] = []
    for i in range(len(candles)):
        if i + 1 < MIN_HISTORY:
            out.append(None)
            continue
        slow, fast = mean(pc, i, TREND_SLOW), mean(pc, i, TREND_FAST)
        slow_before = mean(pc, i - SLOPE, TREND_SLOW)
        close = closes[i]
        if close > slow and fast > slow and slow > slow_before:
            trend = "haussier"
        elif close < slow and fast < slow and slow < slow_before:
            trend = "baissier"
        else:
            trend = "sans tendance"
        long_sd, short_sd = std(i, VOL_LONG), std(i, VOL_SHORT)
        ratio = short_sd / long_sd if long_sd > 0 else 1.0
        volatility = "haute" if ratio > HIGH_VOL else "basse" if ratio < LOW_VOL else "normale"
        avg_volume = mean(pv, i, VOLUME_WINDOW)
        out.append({
            "trend": trend, "volatility": volatility, "vol_ratio": round(ratio, 2),
            "shock": long_sd > 0 and abs(rets[i]) > SHOCK_SIGMA * long_sd,
            "thin": avg_volume > 0 and volumes[i] < THIN_VOLUME * avg_volume,
        })
    return out


def classify(candles: list) -> dict | None:
    """Régime de la dernière bougie close."""
    return regime_series(candles[-(MIN_HISTORY + 50):])[-1] if candles else None


def describe(regime: dict | None) -> str:
    if regime is None:
        return "historique trop court pour qualifier le marché"
    words = {"haussier": "tendance haussière", "baissier": "tendance baissière",
             "sans tendance": "sans tendance nette"}
    parts = [words[regime["trend"]], f"volatilité {regime['volatility']}"]
    if regime["shock"]:
        parts.append("mouvement exceptionnel")
    if regime["thin"]:
        parts.append("volume anormalement faible")
    return ", ".join(parts)


def can_enter(strategy, regime: dict | None) -> tuple[bool, str]:
    """Une nouvelle position est-elle permise dans ce régime ? Fail-closed : un
    régime inconnu interdit l'entrée. Les sorties ne sont jamais bloquées."""
    if regime is None:
        return False, "impossible de qualifier le marché (historique trop court)"
    if regime["shock"]:
        return False, "mouvement exceptionnel sur la dernière bougie"
    if regime["thin"]:
        return False, "volume anormalement faible"
    if regime["trend"] not in strategy.trends:
        return False, f"régime « {regime['trend']} » hors du terrain de la stratégie"
    if regime["volatility"] == "haute" and not strategy.high_vol:
        return False, "volatilité trop haute pour cette stratégie"
    return True, "régime compatible"
