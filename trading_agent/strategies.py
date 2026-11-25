import math
from dataclasses import dataclass, replace
from typing import Callable

from trading_agent.models import Signal, Side, utc_now

def momentum_demo(symbol: str, candles: list, fast=9, slow=21):
    """Toy moving-average crossover for pipeline testing, not a live strategy."""
    if len(candles) < slow + 2:
        return None
    closes = [float(c[4]) for c in candles]
    fast_prev = sum(closes[-fast-1:-1]) / fast
    slow_prev = sum(closes[-slow-1:-1]) / slow
    fast_now = sum(closes[-fast:]) / fast
    slow_now = sum(closes[-slow:]) / slow
    last = closes[-1]
    if fast_prev <= slow_prev and fast_now > slow_now and last > 0:
        recent_low = min(float(c[3]) for c in candles[-14:])
        if recent_low >= last:
            return None
        return Signal(
            symbol=symbol, side=Side.BUY, confidence=0.70,
            reference_price=last, stop_price=recent_low,
            rationale="Demo bullish moving-average crossover; requires validation.",
            timestamp=utc_now()
        )
    return None

def momentum_exit(candles: list, fast=9, slow=21) -> bool:
    """True when the fast average is below the slow one (demo exit rule)."""
    if len(candles) < slow:
        return False
    closes = [float(c[4]) for c in candles]
    return sum(closes[-fast:]) / fast < sum(closes[-slow:]) / slow


# ---------------------------------------------------------------------------
# Indicateurs (calculés uniquement sur des bougies closes)
# ---------------------------------------------------------------------------

def _closes(candles: list) -> list[float]:
    return [float(c[4]) for c in candles]


def sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / n


def ema(values: list[float], n: int) -> float:
    k = 2 / (n + 1)
    result = values[0]
    for value in values[1:]:
        result = value * k + result * (1 - k)
    return result


def atr(candles: list, n: int = 14) -> float:
    """Amplitude moyenne des bougies : mesure la volatilité récente."""
    ranges = []
    for prev, cur in zip(candles[-n - 1:-1], candles[-n:]):
        high, low, prev_close = float(cur[2]), float(cur[3]), float(prev[4])
        ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(ranges) / len(ranges)


def rsi(values: list[float], n: int = 14) -> float:
    """Force relative des hausses sur les n dernières variations (0 à 100)."""
    changes = [b - a for a, b in zip(values[-n - 1:-1], values[-n:])]
    gains = sum(c for c in changes if c > 0)
    losses = -sum(c for c in changes if c < 0)
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def _signal(symbol, name, last, stop, rationale, confidence=0.75):
    if not 0 < stop < last:
        return None
    return Signal(symbol=symbol, side=Side.BUY, confidence=confidence, reference_price=last,
                  stop_price=stop, rationale=rationale, timestamp=utc_now(), strategy=name)


# --- Cassure (breakout) : acheter quand le prix dépasse son plus haut récent ---

def breakout_entry(symbol: str, candles: list, lookback=48, trend=100):
    if len(candles) < max(lookback, trend) + 2:
        return None
    closes = _closes(candles)
    last = closes[-1]
    prior_high = max(float(c[2]) for c in candles[-lookback - 1:-1])
    if last > prior_high and last > sma(closes, trend):
        return _signal(symbol, "breakout", last, last - 2.5 * atr(candles),
                       f"Clôture au-dessus du plus haut des {lookback} dernières bougies, "
                       f"dans une tendance haussière.")
    return None


def breakout_exit(candles: list, lookback=24) -> bool:
    if len(candles) < lookback + 2:
        return False
    return float(candles[-1][4]) < min(float(c[3]) for c in candles[-lookback - 1:-1])


# --- Suivi de tendance : croisement de moyennes, filtré par la tendance longue ---

def trend_entry(symbol: str, candles: list, fast=20, slow=60, trend=200):
    if len(candles) < trend + 2:
        return None
    closes = _closes(candles)
    last = closes[-1]
    crossed = (ema(closes[:-1], fast) <= ema(closes[:-1], slow)
               and ema(closes, fast) > ema(closes, slow))
    if crossed and last > sma(closes, trend):
        return _signal(symbol, "trend", last, last - 3 * atr(candles),
                       "La moyenne rapide passe au-dessus de la lente, "
                       "et le prix est au-dessus de sa moyenne longue.")
    return None


def trend_exit(candles: list, fast=20, slow=60) -> bool:
    if len(candles) < slow + 2:
        return False
    closes = _closes(candles)
    return ema(closes, fast) < ema(closes, slow)


# --- Retour à la moyenne : acheter un creux qui se retourne, en tendance haussière ---

def reversion_entry(symbol: str, candles: list, trend=200, low=30):
    if len(candles) < trend + 2:
        return None
    closes = _closes(candles)
    last = closes[-1]
    if rsi(closes[:-1]) < low <= rsi(closes) and last > sma(closes, trend):
        return _signal(symbol, "reversion", last, last - 2.5 * atr(candles),
                       "Le prix sort d'une zone de survente alors que la tendance longue reste haussière.")
    return None


def reversion_exit(candles: list, high=55) -> bool:
    if len(candles) < 16:
        return False
    return rsi(_closes(candles)) > high


# --- Expansion de volume : forte bougie haussière portée par un volume inhabituel ---

def volume_entry(symbol: str, candles: list, avg=20, mult=2.0, trend=100):
    if len(candles) < trend + 2:
        return None
    closes = _closes(candles)
    last = candles[-1]
    open_, high, low, close, volume = (float(last[i]) for i in (1, 2, 3, 4, 5))
    usual = sum(float(c[5]) for c in candles[-avg - 1:-1]) / avg
    strong = close > open_ and high > low and (close - low) / (high - low) >= 2 / 3
    if usual > 0 and volume >= mult * usual and strong and close > sma(closes, trend):
        return _signal(symbol, "volume_expansion", close, close - 2 * atr(candles),
                       f"Volume {volume / usual:.1f} fois supérieur à la moyenne, bougie haussière "
                       "clôturant dans son tiers haut, au-dessus de la moyenne 100.")
    return None


def volume_exit(candles: list, n=20) -> bool:
    if len(candles) < n + 2:
        return False
    return float(candles[-1][4]) < ema(_closes(candles), n)


# --- Cassure de volatilité : sortie par le haut d'une période de compression ---

def _bandwidth(closes: list[float], n=20) -> float:
    window = closes[-n:]
    mean = sum(window) / n
    sd = (sum((x - mean) ** 2 for x in window) / n) ** 0.5
    return 4 * sd / mean if mean else 0.0


def squeeze_entry(symbol: str, candles: list, n=20, history=120, recent=10):
    if len(candles) < history + n + 2:
        return None
    closes = _closes(candles)
    widths = [_bandwidth(closes[:len(closes) - k], n) for k in range(history)]
    # Compression : la largeur des bandes a touché son plus bas des 120 dernières
    # bougies au cours des 10 dernières.
    compressed = min(widths[:recent]) <= min(widths) * 1.0001
    window = closes[-n:]
    mean = sum(window) / n
    upper = mean + 2 * (sum((x - mean) ** 2 for x in window) / n) ** 0.5
    last = closes[-1]
    if compressed and last > upper:
        return _signal(symbol, "volatility_breakout", last, last - 2 * atr(candles),
                       "Le prix sort par le haut de ses bandes après une période de forte compression.")
    return None


def squeeze_exit(candles: list, n=20) -> bool:
    if len(candles) < n + 2:
        return False
    return float(candles[-1][4]) < sma(_closes(candles), n)


# --- Momentum de série temporelle : suivre le signe du rendement des N dernières bougies ---
# Idée documentée sur les cryptomonnaies : un actif qui a monté sur la période récente
# a tendance à continuer. On entre quand ce rendement redevient positif.

def tsmom_entry(symbol: str, candles: list, n=60):
    if len(candles) < n + 3:
        return None
    closes = _closes(candles)
    now, before = closes[-1] / closes[-1 - n] - 1, closes[-2] / closes[-2 - n] - 1
    if before <= 0 < now:
        last = closes[-1]
        return _signal(symbol, "tsmom", last, last - 3 * atr(candles),
                       f"Le rendement des {n} dernières bougies redevient positif ({now:+.1%}).")
    return None


def tsmom_exit(candles: list, n=60) -> bool:
    if len(candles) < n + 2:
        return False
    closes = _closes(candles)
    return closes[-1] / closes[-1 - n] - 1 < 0


# --- Repli dans une tendance : acheter un retour vers la moyenne 20 en tendance établie ---

def pullback_entry(symbol: str, candles: list, fast=20, mid=50, slow=200):
    if len(candles) < slow + 2:
        return None
    closes = _closes(candles)
    last = candles[-1]
    low, close = float(last[3]), float(last[4])
    e_fast, e_mid, s_slow = ema(closes, fast), ema(closes, mid), sma(closes, slow)
    if e_mid > s_slow and close > e_mid and low <= e_fast < close:
        swing = min(float(c[3]) for c in candles[-5:])
        return _signal(symbol, "pullback", close, min(swing, close - atr(candles)) - 0.5 * atr(candles),
                       "Repli jusqu'à la moyenne 20 puis clôture au-dessus, dans une tendance établie "
                       "(moyenne 50 au-dessus de la 200).")
    return None


def pullback_exit(candles: list, mid=50) -> bool:
    if len(candles) < mid + 2:
        return False
    return float(candles[-1][4]) < ema(_closes(candles), mid)


# --- Candidates figées (Luna, 2026-11-22) : règles dans docs/protocoles/candidates-luna.md ---

def _finite_candles(candles: list, n: int) -> bool:
    if len(candles) < n:
        return False
    for c in candles[-n:]:
        for v in c[1:5]:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0:
                return False
    return True


def _never(candles: list) -> bool:
    return False


def _days_held(history: list, position: dict) -> int | None:
    """Nombre de bougies écoulées depuis la bougie du premier fill (0 le jour du fill)."""
    if len(history) < 2 or not position or position.get("entry_ts") is None:
        return None
    step = history[-1][0] - history[-2][0]
    return int((history[-1][0] - position["entry_ts"]) // step) if step > 0 else None


def _memo_signal(symbol, name, close, distance, memo, rationale):
    if not (distance > 0 and math.isfinite(distance)):
        return None
    stop = close - distance
    if not 0 < stop < close:
        return None
    return Signal(symbol=symbol, side=Side.BUY, confidence=0.75, reference_price=close, stop_price=stop,
                  rationale=rationale, timestamp=utc_now(), strategy=name, memo=memo)


def compression_entry(symbol: str, candles: list):
    if not _finite_candles(candles, 101):
        return None
    prev = candles[-11:-1]
    b, a = max(float(c[2]) for c in prev), min(float(c[3]) for c in prev)
    close, prev_close = float(candles[-1][4]), float(candles[-2][4])
    if (b - a) / prev_close <= 0.08 and close > b and close > sma(_closes(candles), 100):
        return _memo_signal(symbol, "compression_breakout", close, 2 * atr(candles), {"B": b},
                            f"Clôture au-dessus du haut ({b:.6g}) de 10 bougies d'amplitude "
                            f"{(b - a) / prev_close:.1%}, au-dessus de la moyenne 100.")
    return None


def compression_exit_with(history: list, position: dict) -> bool:
    b = ((position or {}).get("memo") or {}).get("B")
    if b is not None and float(history[-1][4]) < b:
        return True
    held = _days_held(history, position)
    return held is not None and held >= 9          # vente à l'ouverture du 10e jour


def dip_entry(symbol: str, candles: list):
    if not _finite_candles(candles, 101):
        return None
    j, j1, j2 = candles[-1], candles[-2], candles[-3]
    drop = float(j1[4]) / float(j2[4]) - 1
    close, low = float(j[4]), float(j[3])
    mid = (float(j1[2]) + float(j1[3])) / 2
    if drop <= -0.05 and close > mid and close > sma(_closes(candles), 100) and low >= float(j1[3]):
        return _memo_signal(symbol, "dip_rebound", close, 2 * atr(candles), {"low_prev": float(j1[3])},
                            f"Reprise après une chute de {drop:.1%} la veille, sans nouveau plus bas, "
                            "au-dessus de la moyenne 100.")
    return None


def dip_exit_with(history: list, position: dict) -> bool:
    low_prev = ((position or {}).get("memo") or {}).get("low_prev")
    if low_prev is not None and float(history[-1][4]) < low_prev:
        return True
    held = _days_held(history, position)
    return held is not None and held >= 4          # vente à l'ouverture du 5e jour


def relative_strength_table(histories: dict[str, list], lookback: int = 90) -> dict:
    """{horodatage: {paire: (rang, rendement)}} seulement les jours où TOUTES les paires
    ont un rendement calculable ; rang 1 = plus forte hausse ; égalités : ordre alphabétique."""
    returns: dict[int, dict[str, float]] = {}
    for symbol, candles in histories.items():
        for i in range(lookback, len(candles)):
            a, b = candles[i - lookback][4], candles[i][4]
            if all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (a, b)):
                returns.setdefault(candles[i][0], {})[symbol] = float(b) / float(a) - 1
    table = {}
    for ts, rets in returns.items():
        if len(rets) == len(histories):
            ordered = sorted(rets, key=lambda s: (-rets[s], s))
            table[ts] = {s: (k + 1, rets[s]) for k, s in enumerate(ordered)}
    return table


def bind_relative_strength(histories: dict[str, list]) -> "Strategy":
    table = relative_strength_table(histories)

    def entry(symbol, candles):
        if not _finite_candles(candles, 101):
            return None
        row = table.get(candles[-1][0])
        if row is None or symbol not in row:
            return None                            # classement incomplet : pas d'entrée
        rank, ret = row[symbol]
        close = float(candles[-1][4])
        if rank <= 3 and ret > 0 and close > sma(_closes(candles), 100):
            return _memo_signal(symbol, "relative_strength", close, 2.5 * atr(candles), {"rank": rank},
                                f"Rang {rank} sur {len(row)} en 90 jours ({ret:+.1%}), au-dessus de la moyenne 100.")
        return None

    def exit_with(history, position):
        if len(history) >= 100 and float(history[-1][4]) < sma(_closes(history), 100):
            return True
        row = table.get(history[-1][0])
        symbol = (position or {}).get("symbol")
        return bool(row and symbol in row and row[symbol][0] >= 5)

    return replace(STRATEGIES["relative_strength"], entry=entry, exit_with=exit_with, universe=None)


# Pistes figées le 2026-11-24 (docs/protocoles/pistes-volume-expansion.md) : backtest seulement.

def breadth_table(histories: dict[str, list], n: int = 100) -> dict:
    """{horodatage: nombre de paires dont la clôture est au-dessus de leur SMA(n)}, seulement
    les jours où la SMA(n) est calculable pour TOUTES les paires."""
    above: dict[int, list[bool]] = {}
    for candles in histories.values():
        closes = _closes(candles)
        for i in range(n - 1, len(candles)):
            window = closes[i - n + 1:i + 1]
            if all(math.isfinite(v) and v > 0 for v in window):
                above.setdefault(candles[i][0], []).append(closes[i] > sum(window) / n)
    return {ts: sum(v) for ts, v in above.items() if len(v) == len(histories)}


def _volume_variant(symbol, candles, name, condition):
    signal = volume_entry(symbol, candles)
    if signal is None or not condition:
        return None
    return replace(signal, strategy=name, rationale=f"{signal.rationale} {condition}")


def bind_volume_breadth(histories: dict[str, list]) -> "Strategy":
    table = breadth_table(histories)

    def entry(symbol, candles):
        count = table.get(candles[-1][0]) if candles else None
        ok = count is not None and count >= 5
        return _volume_variant(symbol, candles, "volume_breadth",
                               ok and f"{count} cryptos sur {len(histories)} au-dessus de leur moyenne 100.")

    return replace(STRATEGIES["volume_breadth"], entry=entry, universe=None)


def bind_volume_leaders(histories: dict[str, list]) -> "Strategy":
    table = relative_strength_table(histories)

    def entry(symbol, candles):
        row = table.get(candles[-1][0]) if candles else None
        ok = bool(row and symbol in row and row[symbol][0] <= 4)
        return _volume_variant(symbol, candles, "volume_leaders",
                               ok and f"Rang {row[symbol][0]} sur {len(row)} en 90 jours.")

    return replace(STRATEGIES["volume_leaders"], entry=entry, universe=None)


# Pistes figées le 2026-11-24 (docs/protocoles/pistes-sortie-horizon-rsi2.md) : backtest seulement.

def chandelier_entry(symbol: str, candles: list):
    signal = volume_entry(symbol, candles)
    return None if signal is None else replace(signal, strategy="volume_chandelier")


def chandelier_exit_with(history: list, position: dict, n: int = 22, mult: float = 3.0) -> bool:
    entry_ts = (position or {}).get("entry_ts")
    if entry_ts is None or len(history) < n + 1:
        return False
    highest = max(float(c[2]) for c in history if c[0] >= entry_ts)
    return float(history[-1][4]) < highest - mult * atr(history, n)


def rsi2_entry(symbol: str, candles: list):
    if not _finite_candles(candles, 201):
        return None
    closes = _closes(candles)
    value, close = rsi(closes, 2), closes[-1]
    if value < 10 and close > sma(closes, 200):
        return _signal(symbol, "rsi2_rebound", close, close - 2 * atr(candles),
                       f"RSI 2 à {value:.0f} (sous 10) au-dessus de la moyenne 200 : repli court en tendance.")
    return None


def rsi2_exit(candles: list) -> bool:
    if len(candles) < 5:
        return False
    return float(candles[-1][4]) > sma(_closes(candles), 5)


@dataclass(frozen=True)
class Strategy:
    name: str
    label: str            # nom affiché
    family: str           # explication en une phrase
    entry: Callable
    exit: Callable
    no_signal: str        # phrase quand il n'y a rien à faire
    exit_reason: str      # phrase quand la sortie se déclenche
    # Terrain de jeu fixé à l'avance (voir regimes.py) : tendances où entrer,
    # et entrée permise ou non en volatilité haute.
    # Choix du 2026-11-17 : la tendance baissière est le seul régime défavorable à
    # toutes les stratégies sur deux paniers de marchés (BTC/ETH/SOL et 5 autres).
    trends: frozenset = frozenset({"haussier", "sans tendance"})
    high_vol: bool = False
    horizon: str = "quelques bougies à quelques jours"
    cancel_if: str = "le prix passe sous le stop avant l'exécution"
    # Extensions (backtest) : sortie qui connaît la position (date d'achat, niveaux notés au
    # signal) ; classement entre plusieurs cryptos ; stratégie réservée au backtest.
    exit_with: Callable | None = None      # exit_with(history, position) -> bool
    universe: Callable | None = None       # universe({paire: bougies}) -> Strategy liée au panier
    backtest_only: bool = False


STRATEGIES: dict[str, Strategy] = {s.name: s for s in [
    Strategy("momentum_demo", "momentum (démo)",
             "Croisement de deux moyennes mobiles courtes. Stratégie de démonstration.",
             momentum_demo, momentum_exit,
             "pas de croisement haussier des moyennes mobiles",
             "la moyenne rapide est repassée sous la moyenne lente"),
    Strategy("breakout", "cassure",
             "Achète quand le prix dépasse son plus haut récent, en tendance haussière.",
             breakout_entry, breakout_exit,
             "le prix n'a pas dépassé son plus haut récent",
             "le prix est passé sous son plus bas récent", high_vol=True),
    Strategy("trend", "suivi de tendance",
             "Achète quand la moyenne rapide passe au-dessus de la lente, en tendance haussière.",
             trend_entry, trend_exit,
             "pas de nouveau départ de tendance",
             "la moyenne rapide est repassée sous la moyenne lente", high_vol=True),
    Strategy("reversion", "retour à la moyenne",
             "Achète un creux qui se retourne quand la tendance longue reste haussière.",
             reversion_entry, reversion_exit,
             "pas de creux en cours de retournement",
             "le prix est revenu vers sa moyenne"),
    Strategy("volume_expansion", "expansion de volume",
             "Achète une forte bougie haussière portée par un volume au moins double de la normale.",
             volume_entry, volume_exit,
             "pas de bougie haussière à volume inhabituel",
             "le prix est repassé sous sa moyenne 20", high_vol=True,
             horizon="quelques bougies, tant que le prix reste au-dessus de sa moyenne 20"),
    # Variante testée à part (2026-11-22) : mêmes règles, mais achats permis en tendance
    # baissière. Piste vue sur le panier officiel, confirmée trade par trade sur 8 autres
    # cryptos ; jugée selon un protocole fixé à l'avance (portefeuille témoin, intervalle
    # par blocs de 28 jours). Ne pilote pas l'essai principal : laboratoire seulement.
    Strategy("volume_expansion_bear", "expansion de volume (baissier permis)",
             "Expansion de volume, achats permis aussi en tendance baissière (variante en test).",
             volume_entry, volume_exit,
             "pas de bougie haussière à volume inhabituel",
             "le prix est repassé sous sa moyenne 20", high_vol=True,
             trends=frozenset({"haussier", "sans tendance", "baissier"}),
             horizon="quelques bougies, tant que le prix reste au-dessus de sa moyenne 20"),
    Strategy("volatility_breakout", "cassure de volatilité",
             "Achète la sortie par le haut d'une période où le marché était inhabituellement calme.",
             squeeze_entry, squeeze_exit,
             "pas de sortie de compression par le haut",
             "le prix est repassé sous sa moyenne 20", high_vol=True,
             horizon="quelques bougies à quelques jours, tant que le mouvement se prolonge"),
    Strategy("tsmom", "momentum de fond",
             "Achète quand le rendement des 60 dernières bougies redevient positif, sort quand il redevient négatif.",
             tsmom_entry, tsmom_exit,
             "le rendement récent n'est pas redevenu positif",
             "le rendement des 60 dernières bougies est redevenu négatif", high_vol=True,
             horizon="plusieurs semaines, tant que la tendance de fond reste positive"),
    Strategy("pullback", "repli en tendance",
             "Achète un repli vers la moyenne 20 quand la tendance de fond est haussière.",
             pullback_entry, pullback_exit,
             "pas de repli vers la moyenne 20 dans une tendance établie",
             "le prix est repassé sous sa moyenne 50",
             horizon="quelques jours, tant que le prix reste au-dessus de sa moyenne 50"),
    # Candidates figées le 2026-11-22 (docs/protocoles/candidates-luna.md) : backtest seulement.
    Strategy("relative_strength", "force relative (candidate)",
             "Achète les 3 cryptos du panier qui ont le plus monté en 90 jours, au-dessus de leur moyenne 100.",
             lambda symbol, candles: None, _never,   # sans panier lié : n'entre jamais
             "pas parmi les 3 plus fortes hausses du panier",
             "rang 5 ou moins bon, ou passage sous la moyenne 100",
             universe=lambda histories: bind_relative_strength(histories), backtest_only=True),
    Strategy("compression_breakout", "cassure après compression (candidate)",
             "Achète la sortie par le haut de 10 bougies étroites (amplitude ≤ 8 %), au-dessus de la moyenne 100.",
             compression_entry, _never,
             "pas de sortie par le haut d'une compression",
             "clôture sous le haut de la compression, ou 10e jour",
             exit_with=compression_exit_with, backtest_only=True),
    Strategy("dip_rebound", "rebond après une chute (candidate)",
             "Achète la reprise qui suit une chute d'au moins 5 % en un jour, au-dessus de la moyenne 100.",
             dip_entry, _never,
             "pas de reprise après une chute de 5 %",
             "clôture sous le plus bas de la veille de l'achat, ou 5e jour",
             exit_with=dip_exit_with, backtest_only=True),
    Strategy("volume_breadth", "expansion de volume, marché large (piste)",
             "Expansion de volume, seulement si au moins 5 des 8 cryptos du panier sont au-dessus de leur moyenne 100.",
             lambda symbol, candles: None, volume_exit,   # sans panier lié : n'entre jamais
             "pas de bougie haussière à volume inhabituel dans un marché large",
             "le prix est repassé sous sa moyenne 20", high_vol=True,
             universe=lambda histories: bind_volume_breadth(histories), backtest_only=True),
    Strategy("volume_leaders", "expansion de volume, cryptos meneuses (piste)",
             "Expansion de volume, seulement sur les 4 cryptos du panier qui ont le plus monté en 90 jours.",
             lambda symbol, candles: None, volume_exit,   # sans panier lié : n'entre jamais
             "pas de bougie haussière à volume inhabituel sur une crypto meneuse",
             "le prix est repassé sous sa moyenne 20", high_vol=True,
             universe=lambda histories: bind_volume_leaders(histories), backtest_only=True),
    Strategy("volume_chandelier", "expansion de volume, sortie suiveuse (piste)",
             "Expansion de volume ; vente quand le prix recule de 3 ATR(22) sous son plus haut depuis l'achat.",
             chandelier_entry, _never,
             "pas de bougie haussière à volume inhabituel",
             "le prix a reculé de 3 ATR sous son plus haut depuis l'achat", high_vol=True,
             exit_with=chandelier_exit_with, backtest_only=True),
    Strategy("rsi2_rebound", "rebond court RSI 2 (piste)",
             "Achète après deux jours de forte baisse (RSI 2 sous 10) au-dessus de la moyenne 200 ; revend au-dessus de la moyenne 5.",
             rsi2_entry, rsi2_exit,
             "pas de repli court en tendance haussière",
             "le prix est repassé au-dessus de sa moyenne 5", backtest_only=True,
             horizon="quelques jours"),
]}


LIVE_STRATEGIES: dict[str, Strategy] = {n: s for n, s in STRATEGIES.items() if not s.backtest_only}


def get_strategy(name: str | None) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"Stratégie inconnue : {name}. Choix : {', '.join(STRATEGIES)}")
    return STRATEGIES[name]
