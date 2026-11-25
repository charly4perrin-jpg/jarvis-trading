"""Règles du Contradicteur, partagées entre l'agent (essai réel) et le backtest.

Une seule source de vérité : l'évaluation sur l'historique applique exactement les
mêmes objections que l'essai, sauf celles qui demandent le carnet d'ordres en
direct (liquidité, déséquilibre), impossibles à rejouer faute d'historique des carnets.
"""
from trading_agent.strategies import STRATEGIES, rsi


def eur(value: float) -> str:
    return f"{value:,.2f} €".replace(",", " ").replace(".", ",")


def data_problems(candles: list, tf_ms: int | None) -> list[tuple[bool, str]]:
    """Contrôle de cohérence des bougies. (majeur ?, description)."""
    problems = []
    if any(float(c[4]) <= 0 or float(c[3]) <= 0 or float(c[2]) < float(c[3]) for c in candles):
        problems.append((True, "bougies incohérentes (prix nul ou plus haut sous le plus bas)"))
    if any(b[0] <= a[0] for a, b in zip(candles, candles[1:])):
        problems.append((True, "horodatages des bougies dans le désordre"))
    elif tf_ms:
        gaps = sum(1 for a, b in zip(candles, candles[1:]) if b[0] - a[0] > tf_ms)
        if gaps:
            problems.append((gaps > len(candles) * 0.02, f"{gaps} bougie(s) manquante(s) dans l'historique"))
    return problems


def objections(signal, candles: list, regime: dict | None, tf_ms: int | None, fee: float, slip: float,
               reward_risk: float, book: dict | None = None, min_depth: float = 0.0) -> list[dict]:
    """Toutes les raisons de douter d'un achat. `book=None` : pas de contrôle du carnet."""
    found: list[dict] = []

    def add(code: str, severity: str, text: str) -> None:
        found.append({"code": code, "severity": severity, "text": text})

    for major, text in data_problems(candles, tf_ms):
        add("donnees", "majeure" if major else "attention", text)
    entry, stop = signal.reference_price, signal.stop_price
    risk = entry - stop
    reward = reward_risk * risk
    cost = entry * 2 * (fee + slip)            # aller-retour : frais + glissement
    if risk <= 0:
        add("rendement_risque", "majeure", "le stop n'est pas sous le prix d'entrée")
    else:
        if cost > 0.5 * reward:
            add("frais", "majeure", f"les coûts ({eur(cost)} par unité) mangent plus de la moitié du gain visé")
        elif cost > 0.25 * reward:
            add("frais", "attention", f"les coûts ({eur(cost)} par unité) dépassent le quart du gain visé")
        net_rr = (reward - cost) / (risk + cost)
        if net_rr < 1.0:
            add("rendement_risque", "majeure", f"rapport gain/perte après coûts de {net_rr:.2f}, sous 1")
    if regime and regime["volatility"] == "haute":
        add("volatilite", "attention", "volatilité anormalement haute : stop plus facilement touché")
    closes = [float(c[4]) for c in candles]
    if len(closes) > 15 and rsi(closes) > 75:
        add("retournement", "attention", f"marché suracheté (RSI {rsi(closes):.0f}) : risque de repli")
    last = candles[-1]
    high, low, close = float(last[2]), float(last[3]), float(last[4])
    if high > low and (close - low) / (high - low) < 0.5:
        add("faux_signal", "attention", "la bougie du signal clôture dans sa moitié basse")
    volumes = [float(c[5]) for c in candles[-21:-1] if len(c) > 5]
    if volumes and float(last[5]) < sum(volumes) / len(volumes):
        add("faux_signal", "attention", "volume de la bougie du signal sous sa moyenne récente")
    against = [st.label for st in STRATEGIES.values() if st.name != signal.strategy and st.exit(candles)]
    if len(against) >= 2:
        add("contradiction", "attention", "d'autres stratégies donnent un signal de sortie : " + ", ".join(against))
    if book is not None:
        bids, asks = book.get("bids") or [], book.get("asks") or []
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2
            ask_depth = sum(p * q for p, q, *_ in asks if p <= mid * 1.005)
            bid_depth = sum(p * q for p, q, *_ in bids if p >= mid * 0.995)
            if ask_depth < min_depth:
                add("liquidite", "majeure", f"seulement {eur(ask_depth)} à la vente à moins de 0,5 % du prix")
            if bid_depth > 0 and ask_depth > 3 * bid_depth:
                add("carnet", "attention", "carnet déséquilibré : trois fois plus de vendeurs que d'acheteurs")
        else:
            add("liquidite", "majeure", "carnet d'ordres vide")
    return found
