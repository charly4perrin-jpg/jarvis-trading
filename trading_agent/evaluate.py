"""Évaluation rigoureuse des stratégies et circuit de promotion.

Évaluation : chaque stratégie est rejouée sur un panier de marchés, et la période
est découpée en fenêtres successives. Une stratégie n'est retenue que si elle
tient sur l'ensemble : assez de trades, espérance nette positive, aucune fenêtre
en perte, ratio gains/pertes et perte maximale dans les limites.

Promotion : candidate -> backtest validé -> papier validé -> éligible au réel.
Chaque passage exige une preuve chiffrée ET une action humaine explicite.
Aucun agent ne peut promouvoir une stratégie : le registre n'est modifié que
par la ligne de commande ou l'interface, et chaque changement est journalisé.
"""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading_agent.backtest import max_drawdown, run_backtest, sharpe
from trading_agent.config import Settings
from trading_agent.regimes import TRENDS, VOLATILITIES
from trading_agent.strategies import STRATEGIES, Strategy, get_strategy

DEFAULT_BASKET = ["BTC/EUR", "ETH/EUR", "SOL/EUR"]
STATUSES = ["candidate", "backtest_ok", "paper_ok", "eligible"]
STATUS_LABELS = {"candidate": "candidate", "backtest_ok": "backtest validé",
                 "paper_ok": "papier validé", "eligible": "éligible au réel"}
EVIDENCE_MAX_AGE_DAYS = 7
PAPER_MIN_WEEKS = 8
PAPER_MIN_TRADES = 20
REGIME_MIN_TRADES = 20


def _pf(pnls: list[float]) -> float | None:
    loss = -sum(p for p in pnls if p <= 0)
    return sum(p for p in pnls if p > 0) / loss if loss > 0 else None


def regime_breakdown(trades: list[dict]) -> list[dict]:
    """Résultat des trades selon le régime au moment de l'entrée, sur chaque moitié
    de la période : un régime n'est jugé favorable que s'il l'est sur les deux."""
    def avg(xs):
        return sum(xs) / len(xs) if xs else None
    rows = []
    for axis, values in (("trend", TRENDS), ("volatility", VOLATILITIES)):
        for value in values:
            sel = [t for t in trades if t["regime"] and t["regime"][axis] == value]
            pnls = [t["pnl"] for t in sel]
            halves = [avg([t["pnl"] for t in sel if t["half"] == h]) for h in (0, 1)]
            if len(sel) < REGIME_MIN_TRADES:
                verdict = "trop peu de trades pour conclure"
            elif all(h is not None and h > 0 for h in halves):
                verdict = "positif sur les deux moitiés"
            elif all(h is not None and h < 0 for h in halves):
                verdict = "négatif sur les deux moitiés"
            else:
                verdict = "instable d'une moitié à l'autre"
            rows.append({"axis": axis, "value": value, "trades": len(sel),
                         "expectancy": avg(pnls), "total": sum(pnls), "profit_factor": _pf(pnls),
                         "first_half": halves[0], "second_half": halves[1], "verdict": verdict})
    return rows


def _criterion(key: str, label: str, value: str, passed: bool) -> dict:
    return {"key": key, "label": label, "value": value, "passed": bool(passed)}


def evaluate_strategy(histories: dict[str, dict], settings: Settings, tf_seconds: int,
                      strategy: str, windows: int = 6) -> dict:
    """`histories` : {paire: {"candles": [...], "limits": {...}, "fee": taux}}.
    Chaque paire est rejouée séparément avec le même capital de départ."""
    strat = strategy if isinstance(strategy, Strategy) else get_strategy(strategy)
    if strat.universe is not None:   # classement entre les paires du panier (force relative)
        strat = strat.universe({s: h["candles"] for s, h in histories.items()})
    pnls: list[float] = []
    window_returns = [[] for _ in range(windows)]
    window_trades = [0] * windows
    per_symbol = []
    fees = 0.0
    raw_trades: list[dict] = []
    for symbol, history in histories.items():
        candles = history["candles"]
        local = settings.model_copy(update={"paper_fee_rate": history.get("fee") or settings.paper_fee_rate})
        report = run_backtest(candles, local, symbol, tf_seconds, history.get("limits"), strategy=strat)
        curve, n = report["equity_curve"], len(candles)
        sells = [f for f in report["fills"] if f["side"] == "sell"]
        pnls += [f["realized_pnl"] for f in sells]
        # Analyse par régime : sur une série SANS filtre, pour voir où la stratégie
        # gagne et perd réellement, y compris là où le filtre l'empêcherait d'entrer.
        raw = report if not local.regime_filter else run_backtest(
            candles, local, symbol, tf_seconds, history.get("limits"), strategy=strat,
            regime_filter=False)
        half_ts = candles[len(candles) // 2][0]
        raw_trades += [{"pnl": f["realized_pnl"], "regime": f.get("entry_regime"),
                        "half": 0 if f["ts_ms"] < half_ts else 1}
                       for f in raw["fills"] if f["side"] == "sell"]
        fees += report["fees"]
        bounds = [round(i * n / windows) for i in range(windows + 1)]
        for k, (a, b) in enumerate(zip(bounds, bounds[1:])):
            start = curve[a - 1] if a else report["start_equity"]
            window_returns[k].append(curve[b - 1] / start - 1)
            end_ts = candles[b][0] if b < n else math.inf
            window_trades[k] += sum(1 for f in sells if candles[a][0] <= f["ts_ms"] < end_ts)
        per_symbol.append({"symbol": symbol, "return": report["return"], "trades": len(sells),
                           "max_drawdown": max_drawdown(curve), "sharpe": sharpe(curve, tf_seconds),
                           "buy_and_hold": report["buy_and_hold_return"]})

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    trades = len(pnls)
    expectancy = sum(pnls) / trades if trades else None
    profit_factor = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None
    drawdown = max((s["max_drawdown"] for s in per_symbol), default=0.0)
    returns = [sum(r) / len(r) for r in window_returns]
    gaining = sum(1 for r in returns if r > 0)
    losing = sum(1 for r in returns if r < 0)
    s = settings
    criteria = [
        _criterion("trades", f"Au moins {s.accept_min_trades} trades clôturés",
                   f"{trades} trades", trades >= s.accept_min_trades),
        _criterion("expectancy", "Espérance nette positive par trade",
                   "aucun trade" if expectancy is None else f"{expectancy:+.2f} € par trade",
                   expectancy is not None and expectancy > 0),
        _criterion("windows", f"Aucune des {windows} fenêtres en perte, au moins la moitié en gain",
                   f"{gaining} en gain, {losing} en perte", losing == 0 and gaining * 2 >= windows),
        _criterion("profit_factor", f"Ratio gains/pertes d'au moins {s.accept_min_profit_factor:g}",
                   "n/a" if profit_factor is None else f"{profit_factor:.2f}",
                   profit_factor is not None and profit_factor >= s.accept_min_profit_factor),
        _criterion("drawdown", f"Perte maximale sous {s.accept_max_drawdown:.0%}",
                   f"-{drawdown:.1%}", drawdown <= s.accept_max_drawdown),
    ]
    return {
        "kind": "backtest", "strategy": strat.name, "label": strat.label,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": list(histories), "tf_seconds": tf_seconds,
        "candles": sum(len(h["candles"]) for h in histories.values()),
        "trades": trades, "win_rate": len(wins) / trades if trades else None,
        "expectancy": expectancy, "profit_factor": profit_factor, "max_drawdown": drawdown,
        "return": sum(x["return"] for x in per_symbol) / len(per_symbol) if per_symbol else 0.0,
        "fees": fees, "window_returns": returns, "window_trades": window_trades,
        "per_symbol": per_symbol, "criteria": criteria,
        "regime_filter": settings.regime_filter,
        "unfiltered": {"trades": len(raw_trades),
                       "expectancy": (sum(t["pnl"] for t in raw_trades) / len(raw_trades)
                                      if raw_trades else None),
                       "profit_factor": _pf([t["pnl"] for t in raw_trades])},
        "by_regime": regime_breakdown(raw_trades),
        "passed": all(c["passed"] for c in criteria),
    }


def add_deflated_criterion(report: dict, returns: list[float], all_sharpes: list[float],
                           n_trials: int, threshold: float) -> dict:
    """Ajoute le critère du Sharpe dégonflé (portefeuille à capital partagé) : l'avantage
    doit résister au nombre total de configurations essayées."""
    from trading_agent.overfitting import deflated_sharpe
    d = deflated_sharpe(returns, [s for s in all_sharpes if s is not None], n_trials)
    value = d["dsr"]
    report["deflated_sharpe"] = {**d, "threshold": threshold}
    report["criteria"].append(_criterion(
        "deflated", f"Sharpe dégonflé d'au moins {threshold:.2f} ({n_trials} essais comptés)",
        "n/a" if value is None else f"{value:.2f}", value is not None and value >= threshold))
    report["passed"] = all(c["passed"] for c in report["criteria"])
    return report


def paper_evidence(row: dict, since: str, now: datetime | None = None) -> dict:
    """Preuve de l'étape papier, à partir du classement du laboratoire."""
    now = now or datetime.now(timezone.utc)
    weeks = (now - datetime.fromisoformat(since)).days / 7
    criteria = [
        _criterion("weeks", f"Au moins {PAPER_MIN_WEEKS} semaines de paper trading",
                   f"{weeks:.1f} semaines", weeks >= PAPER_MIN_WEEKS),
        _criterion("trades", f"Au moins {PAPER_MIN_TRADES} trades clôturés",
                   f"{row['trades']} trades", row["trades"] >= PAPER_MIN_TRADES),
        _criterion("return", "Résultat net positif",
                   f"{row['return']:+.2%}", row["return"] > 0),
    ]
    return {"kind": "paper", "strategy": row["name"], "evaluated_at": now.isoformat(),
            "criteria": criteria, "passed": all(c["passed"] for c in criteria)}


class Registry:
    """Statut de chaque stratégie dans le circuit de validation."""

    def __init__(self, path: str, evaluations_dir: str):
        self.path = Path(path)
        self.dir = Path(evaluations_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def status(self, name: str) -> str:
        return self.data.get(name, {}).get("status", "candidate")

    def history(self, name: str) -> list[dict]:
        return self.data.get(name, {}).get("history", [])

    def save_evaluation(self, report: dict) -> None:
        (self.dir / f"{report['strategy']}.json").write_text(
            json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8")

    def latest_evaluation(self, name: str) -> dict | None:
        path = self.dir / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def next_step(self, name: str) -> str | None:
        i = STATUSES.index(self.status(name))
        return STATUSES[i + 1] if i + 1 < len(STATUSES) else None

    def promote(self, name: str, evidence: dict | None, by: str) -> str:
        """Fait avancer une stratégie d'UN cran. Refuse sans preuve valide.
        `by` identifie la personne : la promotion n'est jamais automatique."""
        get_strategy(name)
        target = self.next_step(name)
        if target is None:
            raise PermissionError("Cette stratégie est déjà au dernier statut.")
        if not by:
            raise PermissionError("Une promotion doit être décidée par une personne.")
        needed = {"backtest_ok": "backtest", "paper_ok": "paper"}.get(target)
        if needed:
            if not evidence or evidence.get("kind") != needed or evidence.get("strategy") != name:
                raise PermissionError("Aucune évaluation disponible pour cette étape.")
            if not evidence.get("passed"):
                failed = [c["label"] for c in evidence["criteria"] if not c["passed"]]
                raise PermissionError("Critères non remplis : " + " ; ".join(failed) + ".")
            age = datetime.now(timezone.utc) - datetime.fromisoformat(evidence["evaluated_at"])
            if age > timedelta(days=EVIDENCE_MAX_AGE_DAYS):
                raise PermissionError("L'évaluation date de plus de 7 jours : relancez-la.")
        self._set(name, target, by, "promotion",
                  [c["value"] for c in evidence["criteria"]] if evidence else [])
        return target

    def demote(self, name: str, by: str, reason: str) -> str:
        get_strategy(name)
        self._set(name, "candidate", by, f"rétrogradation : {reason}", [])
        return "candidate"

    def _set(self, name: str, status: str, by: str, action: str, evidence: list) -> None:
        entry = self.data.setdefault(name, {"status": "candidate", "history": []})
        entry["history"].append({"ts": datetime.now(timezone.utc).isoformat(), "from": entry["status"],
                                 "to": status, "by": by, "action": action, "evidence": evidence})
        entry["status"] = status
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def overview(self) -> list[dict]:
        return [{"name": name, "label": strat.label, "status": self.status(name),
                 "status_label": STATUS_LABELS[self.status(name)], "next": self.next_step(name),
                 "next_label": STATUS_LABELS.get(self.next_step(name)),
                 "evaluation": self.latest_evaluation(name), "history": self.history(name)}
                for name, strat in STRATEGIES.items()]


def format_evaluation(r: dict) -> str:
    lines = [f"ÉVALUATION {r['label']} sur {', '.join(r['symbols'])} ({r['candles']} bougies)",
             f"  Rendement moyen {r['return']:+.2%}, {r['trades']} trades, frais {r['fees']:.0f} €",
             "  Fenêtres : " + "  ".join(f"{x:+.1%}" for x in r["window_returns"])]
    for c in r["criteria"]:
        lines.append(f"  [{'OK ' if c['passed'] else 'NON'}] {c['label']} : {c['value']}")
    if r.get("by_regime"):
        u = r["unfiltered"]
        filt = "avec filtre de régime" if r.get("regime_filter") else "sans filtre de régime"
        lines.append(f"  Critères calculés {filt}. Sans filtre : {u['trades']} trades, "
                     + ("n/a" if u["expectancy"] is None else f"{u['expectancy']:+.2f} € par trade"))
        lines.append("  Par régime à l'entrée (sans filtre) : trades, espérance, 1re / 2e moitié")
        for row in r["by_regime"]:
            def money(x):
                return "  n/a " if x is None else f"{x:+6.2f}"
            name = row["value"] if row["axis"] == "trend" else f"volatilité {row['value']}"
            lines.append(f"    {name:<18} {row['trades']:>4}  {money(row['expectancy'])}  "
                         f"{money(row['first_half'])} / {money(row['second_half'])}  {row['verdict']}")
    lines.append("  => " + ("CRITÈRES REMPLIS" if r["passed"] else "critères non remplis"))
    return "\n".join(lines)
