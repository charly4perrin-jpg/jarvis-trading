"""Mesure de la performance réelle (papier) et détection de dégradation.

Tout est calculé après frais et glissement, à partir des journaux d'ordres fictifs
de chaque plateforme et de l'historique du capital.

Dégradation d'une stratégie (règles fixées à l'avance) :
- au moins 10 trades clôturés en papier ;
- et l'une des deux conditions :
  * les 10 derniers trades ont une espérance négative ET un ratio gains/pertes
    sous 0,7 ;
  * l'espérance en papier est nettement sous celle du backtest : écart de plus de
    deux erreurs-types (un écart aussi grand a peu de chances d'être du hasard).
Une stratégie dégradée est mise en pause : elle n'ouvre plus de position. Les
positions déjà ouvertes gardent leurs stops et leurs sorties. Seule une personne
peut lever la pause (le système se met en sécurité, il ne se relance pas seul).
"""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

DEGRADATION_MIN_TRADES = 10
RECENT_TRADES = 10
RECENT_MIN_PROFIT_FACTOR = 0.7


def load_fills(path: Path | None) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def trade_stats(pnls: list[float]) -> dict:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(pnls)
    mean = sum(pnls) / n if n else None
    sd = math.sqrt(sum((p - mean) ** 2 for p in pnls) / (n - 1)) if n > 1 else None
    return {"trades": n, "wins": len(wins), "win_rate": len(wins) / n if n else None,
            "net": sum(pnls), "expectancy": mean, "stdev": sd,
            "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None}


def curve_stats(points: list[tuple[str, float]], periods_per_year: float) -> dict:
    """Rendement, perte maximale, Sharpe et Sortino annualisés d'une courbe de capital."""
    values = [v for _, v in points]
    if len(values) < 2:
        return {"return": None, "max_drawdown": 0.0, "sharpe": None, "sortino": None, "points": len(values)}
    peak, drawdown = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        drawdown = max(drawdown, 1 - v / peak if peak > 0 else 0.0)
    rets = [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
    down = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets))
    scale = math.sqrt(periods_per_year)
    return {"return": values[-1] / values[0] - 1, "max_drawdown": drawdown,
            "sharpe": mean / sd * scale if sd > 0 else None,
            "sortino": mean / down * scale if down > 0 else None, "points": len(values)}


def costs(fills: list[dict]) -> dict:
    fees = sum(f.get("fee", 0.0) for f in fills)
    slippage = sum(abs(f.get("slippage_bps", 0.0)) / 10_000 * f["quantity"] * f["fill_price"]
                   for f in fills if f.get("quantity") and f.get("fill_price"))
    return {"fees": fees, "slippage": slippage}


def degradation(pnls: list[float], backtest_expectancy: float | None) -> list[str]:
    """Raisons de considérer une stratégie comme dégradée (liste vide = rien à signaler)."""
    if len(pnls) < DEGRADATION_MIN_TRADES:
        return []
    reasons = []
    recent = trade_stats(pnls[-RECENT_TRADES:])
    pf = recent["profit_factor"]
    if recent["expectancy"] < 0 and (pf is None or pf < RECENT_MIN_PROFIT_FACTOR):
        reasons.append(f"les {RECENT_TRADES} derniers trades perdent en moyenne "
                       f"{-recent['expectancy']:.2f} € (ratio gains/pertes "
                       f"{'nul' if pf is None else f'{pf:.2f}'})")
    stats = trade_stats(pnls)
    if backtest_expectancy is not None and stats["stdev"]:
        gap = stats["expectancy"] - backtest_expectancy
        if gap < -2 * stats["stdev"] / math.sqrt(stats["trades"]):
            reasons.append(f"espérance en papier {stats['expectancy']:+.2f} € par trade, nettement sous "
                           f"celle du backtest ({backtest_expectancy:+.2f} €)")
    return reasons


def analyse(venues: list[dict], periods_per_year: float,
            backtest: dict[str, float | None] | None = None) -> dict:
    """`venues` : [{"exchange", "fills", "positions", "equity", "start", "curve"}]."""
    backtest = backtest or {}
    by_strategy: dict[str, list[float]] = {}
    by_market: dict[str, list[float]] = {}
    rows = []
    for v in venues:
        sells = [f for f in v["fills"] if f.get("side") == "sell"]
        pnls = [f["realized_pnl"] for f in sells]
        for f in sells:
            by_strategy.setdefault(f.get("strategy") or "inconnue", []).append(f["realized_pnl"])
            by_market.setdefault(f"{f['symbol']} ({v['exchange']})", []).append(f["realized_pnl"])
        unrealized = sum(p["quantity"] * (p["mark_price"] - p["entry_price"]) - p.get("entry_fee", 0.0)
                         for p in v["positions"].values())
        rows.append({"exchange": v["exchange"], "equity": v["equity"], "start": v["start"],
                     "net_return": v["equity"] / v["start"] - 1 if v["start"] else None,
                     "realized": sum(pnls), "unrealized": unrealized, **costs(v["fills"]),
                     "trades": trade_stats(pnls), "curve": curve_stats(v["curve"], periods_per_year)})
    strategies = {}
    for name, pnls in by_strategy.items():
        strategies[name] = {**trade_stats(pnls), "backtest_expectancy": backtest.get(name),
                            "degradation": degradation(pnls, backtest.get(name))}
    return {"at": datetime.now(timezone.utc).isoformat(), "exchanges": rows,
            "strategies": strategies,
            "markets": {k: trade_stats(p) for k, p in by_market.items()}}


class PauseList:
    """Stratégies mises en pause (fichier partagé entre serveur et ligne de commande)."""

    def __init__(self, path: str):
        self.path = Path(path)

    def all(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def is_paused(self, name: str) -> bool:
        return name in self.all()

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def pause(self, name: str, reason: str, by: str) -> None:
        data = self.all()
        data[name] = {"since": datetime.now(timezone.utc).isoformat(), "reason": reason, "by": by}
        self._write(data)

    def resume(self, name: str, by: str) -> dict:
        """Lever la pause : décision humaine (`by` obligatoire)."""
        if not by:
            raise PermissionError("Seule une personne peut lever la pause d'une stratégie.")
        data = self.all()
        entry = data.pop(name, None)
        if entry is None:
            raise ValueError(f"La stratégie {name} n'est pas en pause.")
        self._write(data)
        return entry
