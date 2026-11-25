"""Robustesse Monte-Carlo : le résultat d'une stratégie tient-il à l'ordre des trades ?

Un seul historique de trades donne un seul rendement et une seule perte maximale.
On rejoue ici ces trades des milliers de fois :
- rééchantillonnage : tirage avec remise des trades clôturés (« bootstrap ») ou simple
  permutation de leur ordre (« shuffle ») ;
- frais bruités : les frais payés (entrée + sortie) sont multipliés par un facteur tiré
  au hasard entre `fee_low` et `fee_high` ;
- glissement bruité : un glissement supplémentaire entre 0 et `extra_slippage_bps` est
  retiré sur chaque jambe (achat et vente).
On en tire un intervalle de confiance du rendement et de la perte maximale.

Idée classique (percentiles 5/95, probabilité de perte) réécrite ici sans recopier de
code. Lecture seule : aucun ordre, aucun effet sur les décisions. En cas de données
douteuses ou trop peu nombreuses, le module refuse de conclure (statut « insuffisant »
ou « refusé ») plutôt que de donner un intervalle trompeur.
"""
import json
import math
import random
from pathlib import Path

from trading_agent.backtest import max_drawdown
from trading_agent.strategies import LIVE_STRATEGIES as STRATEGIES

MIN_TRADES = 10
RELIABLE_TRADES = 30          # en dessous, la fourchette est probablement trop étroite
MAX_RUNS = 20_000
METHODS = ("bootstrap", "shuffle")


def _finite(*values) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               for v in values)


def trades_from_fills(fills: list[dict]) -> list[dict] | None:
    """Trades clôturés (ventes) d'un journal de fills papier.
    Retourne None si une vente est incomplète ou contient une valeur non finie."""
    trades = []
    for f in fills:
        if f.get("side") != "sell":
            continue
        pnl, fee, entry_fee = f.get("realized_pnl"), f.get("fee"), f.get("entry_fee", 0.0)
        qty, entry, exit_ = f.get("quantity"), f.get("entry_price"), f.get("fill_price")
        if not _finite(pnl, fee, entry_fee, qty, entry, exit_) or min(fee, entry_fee, qty, entry, exit_) < 0:
            return None
        trades.append({"pnl": float(pnl), "fees": float(fee + entry_fee),
                       "notional": float(qty * entry + qty * exit_)})
    return trades


def percentile(sorted_values: list[float], q: float) -> float:
    """Percentile par interpolation linéaire (q entre 0 et 1) d'une liste triée."""
    pos = (len(sorted_values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _refuse(status: str, reason: str, n: int = 0) -> dict:
    return {"status": status, "reason": reason, "trades": n}


def simulate(trades: list[dict] | None, start_equity: float, runs: int = 2000, seed: int = 0,
             method: str = "bootstrap", fee_low: float = 0.8, fee_high: float = 1.5,
             extra_slippage_bps: float = 10.0, confidence: float = 0.90,
             min_trades: int = MIN_TRADES) -> dict:
    """Intervalle de confiance du rendement et de la perte maximale par Monte-Carlo."""
    if trades is None:
        return _refuse("refusé", "journal de trades illisible ou incohérent")
    entiers = all(isinstance(v, int) and not isinstance(v, bool) for v in (runs, min_trades)) \
        and (seed is None or (isinstance(seed, int) and not isinstance(seed, bool)))
    if not entiers:
        return _refuse("refusé", "paramètres de simulation invalides", len(trades))
    if method not in METHODS:
        return _refuse("refusé", f"méthode inconnue : {method}", len(trades))
    if not (_finite(start_equity, fee_low, fee_high, extra_slippage_bps, confidence)
            and start_equity > 0 and 0 <= fee_low <= fee_high and extra_slippage_bps >= 0
            and 0 < confidence < 1 and isinstance(runs, int) and 1 <= runs <= MAX_RUNS):
        return _refuse("refusé", "paramètres de simulation invalides", len(trades))
    if len(trades) < max(2, min_trades):
        return _refuse("insuffisant", f"{len(trades)} trades clôturés, il en faut {max(2, min_trades)}",
                       len(trades))

    warnings = ["Gains rejoués en euros fixes alors que la taille des positions suit le capital : "
                "perte maximale et risque de tout perdre approximatifs."]
    if len(trades) < RELIABLE_TRADES:
        warnings.append(f"Seulement {len(trades)} trades (moins de {RELIABLE_TRADES}) : "
                        "fourchette probablement trop étroite.")
    if method == "shuffle":
        warnings.append("Ordre mélangé : le rendement final ne varie qu'avec les frais et le glissement ; "
                        "seule la perte maximale reflète l'ordre des trades.")
    rng = random.Random(seed)
    returns, drawdowns, ruined = [], [], 0
    for _ in range(runs):
        sample = (rng.choices(trades, k=len(trades)) if method == "bootstrap"
                  else rng.sample(trades, len(trades)))
        equity, curve = start_equity, [start_equity]
        for t in sample:
            fee_factor = rng.uniform(fee_low, fee_high)
            slip = rng.uniform(0.0, extra_slippage_bps) / 10_000
            equity += t["pnl"] - (fee_factor - 1) * t["fees"] - slip * t["notional"]
            curve.append(max(equity, 0.0))
            if equity <= 0:
                ruined += 1
                break
        returns.append(curve[-1] / start_equity - 1)
        drawdowns.append(max_drawdown(curve))

    returns.sort()
    drawdowns.sort()
    tail = (1 - confidence) / 2
    observed = [start_equity]
    for t in trades:
        observed.append(observed[-1] + t["pnl"])
    return {
        "status": "ok", "trades": len(trades), "runs": runs, "method": method,
        "confidence": confidence,
        "observed_return": observed[-1] / start_equity - 1,
        "observed_max_drawdown": max_drawdown(observed),
        "return": {"low": percentile(returns, tail), "median": percentile(returns, 0.5),
                   "high": percentile(returns, 1 - tail)},
        # Perte maximale : le cas défavorable est le haut de la distribution.
        "max_drawdown": {"low": percentile(drawdowns, tail), "median": percentile(drawdowns, 0.5),
                         "high": percentile(drawdowns, 1 - tail)},
        "prob_loss": sum(1 for r in returns if r < 0) / runs,
        "prob_ruin": ruined / runs,
        "warnings": warnings,
    }


def load_ledger(path: Path) -> list[dict] | None:
    """Fills d'un journal JSONL ; None si une ligne est illisible."""
    if not path.exists():
        return []
    fills = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        return None
                    fills.append(item)
    except (OSError, ValueError):
        return None
    return fills


def lab_robustness(directory: str, start_equity: float, **options) -> list[dict]:
    """Robustesse de chaque stratégie du laboratoire, à partir de ses journaux papier."""
    rows = []
    for name, strat in STRATEGIES.items():
        fills = load_ledger(Path(directory) / f"{name}_ledger.jsonl")
        trades = None if fills is None else trades_from_fills(fills)
        rows.append({"name": name, "label": strat.label,
                     **simulate(trades, start_equity, **options)})
    return rows


def format_robustness(rows: list[dict]) -> str:
    lines = ["ROBUSTESSE MONTE-CARLO (trades rééchantillonnés, frais et glissement bruités)"]
    for r in rows:
        if r["status"] != "ok":
            lines.append(f"{r['label']:<24} non concluant : {r['reason']}")
            continue
        ret, dd = r["return"], r["max_drawdown"]
        lines.append(
            f"{r['label']:<24} rendement {ret['low']:+.1%} à {ret['high']:+.1%} "
            f"(médiane {ret['median']:+.1%}), perte max jusqu'à -{dd['high']:.1%}, "
            f"proba de perte {r['prob_loss']:.0%} — {r['trades']} trades, "
            f"intervalle {r['confidence']:.0%}")
        lines.extend(f"    ! {w}" for w in r.get("warnings", []))
    lines.append("Attention : ces intervalles supposent que les trades passés sont représentatifs.")
    return "\n".join(lines)
