"""Rapport de performance détaillé (« tearsheet ») des stratégies du laboratoire et du mode Shadow.

Lecture seule : on lit les journaux et fichiers d'état, on n'écrit rien et on n'appelle
aucune fonction qui écrit (pas de `Lab(...)`, pas de `ShadowBook.mark_reference`).

Méthode (inspirée de quantstats, ranaroussi/quantstats, Apache-2.0, et de pyfolio,
quantopian/pyfolio, Apache-2.0 — formules réécrites ici sans dépendance) :
- la courbe de capital est rééchantillonnée sur TOUS les jours du calendrier (UTC) entre
  le début et `now`, en recopiant la dernière valeur les jours sans trade ; sinon
  Sharpe, Sortino et volatilité seraient gonflés ;
- le premier point est le capital AVANT le premier jour (capital initial) : un trade du
  jour de début compte donc dans tous les indicateurs ;
- Sharpe = moyenne / écart-type (n-1) des rendements journaliers × √365 ;
  Sortino = moyenne / √(Σ min(r, 0)² / n) × √365 ; volatilité = écart-type × √365 ;
  rendement annualisé = (fin / début)^(365 / jours) − 1 ; Calmar = annualisé / perte max ;
- perte maximale = plus forte baisse depuis un sommet ; durée = plus longue suite de
  jours sous le dernier sommet.
Les courbes ne comptent que le réalisé (ventes du laboratoire, trades Shadow clos) :
le rapport l'écrit en clair, car la perte maximale et sa durée sont alors sous-estimées
et le Calmar flatteur.
"""
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

PERIODS_PER_YEAR = 365          # la crypto cote tous les jours
MAX_RISK_PER_TRADE = 0.01       # même plafond que la configuration (risk_per_trade_pct ≤ 1 %)
SHORT_PERIOD_DAYS = 30

NOTE_REALIZED = ("Courbe construite sur le seul résultat réalisé (positions ouvertes non "
                 "valorisées) : perte maximale et durée sous-estimées, Calmar flatteur.")
NOTE_MIXED = ("Comparaison à acheter-et-garder : la stratégie est mesurée en réalisé, la "
              "référence aux prix de marché ; l'écart n'est pas homogène.")


def _number(value, what: str) -> float:
    """Nombre fini, jamais un booléen ; sinon refus."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{what} invalide : {value!r}")
    return float(value)


def _aware(value, what: str) -> datetime:
    """Date avec fuseau obligatoire (texte ISO ou datetime)."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{what} illisible : {value!r}") from exc
    if not isinstance(value, datetime):
        raise ValueError(f"{what} invalide : {value!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{what} sans fuseau horaire : {value!r}")
    return value


def _utc_day(moment: datetime) -> date:
    return (moment - moment.utcoffset()).date()


def read_ledger(path) -> tuple[list[dict], int]:
    """Lignes du journal qui sont des objets JSON, et nombre de lignes ignorées."""
    path = Path(path)
    if not path.exists():
        return [], 0
    rows, ignored = [], 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                ignored += 1
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                ignored += 1
    return rows, ignored


def daily_curve(start_capital: float, pnls: list[tuple[datetime, float]],
                since: datetime, now: datetime) -> list[tuple[str, float]]:
    """Capital initial, puis capital de fin de chaque jour du calendrier de `since` à `now`."""
    start_capital = _number(start_capital, "capital initial")
    if start_capital <= 0:
        raise ValueError("Le capital initial doit être positif.")
    first, last = _utc_day(since), _utc_day(now)
    if last < first:
        raise ValueError("`now` est antérieur au début de la période.")
    by_day: dict[date, float] = {}
    for moment, pnl in pnls:
        day = _utc_day(moment)
        if first <= day <= last:
            by_day[day] = by_day.get(day, 0.0) + pnl
    curve, value, day = [("capital initial", start_capital)], start_capital, first
    while day <= last:
        value += by_day.get(day, 0.0)
        curve.append((day.isoformat(), value))
        day += timedelta(days=1)
    return curve


def _drawdown(values: list[float]) -> tuple[float, int]:
    peak, worst, run, longest = values[0], 0.0, 0, 0
    for v in values[1:]:
        if v >= peak:
            peak, run = v, 0
        else:
            run += 1
            longest = max(longest, run)
            worst = max(worst, 1 - v / peak if peak > 0 else 1.0)
    return worst, longest


def curve_metrics(values: list[float]) -> dict:
    """Indicateurs d'une courbe journalière dont le premier point est le capital initial."""
    if len(values) < 2 or values[0] <= 0:
        raise ValueError("Courbe trop courte ou capital initial nul.")
    days = len(values) - 1
    total = values[-1] / values[0] - 1
    rets = [b / a - 1 if a > 0 else -1.0 for a, b in zip(values, values[1:])]
    mean = sum(rets) / days
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (days - 1)) if days > 1 else 0.0
    down = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / days)
    scale = math.sqrt(PERIODS_PER_YEAR)
    annual = (values[-1] / values[0]) ** (PERIODS_PER_YEAR / days) - 1 if values[-1] > 0 else -1.0
    max_dd, dd_days = _drawdown(values)
    return {"days": days, "start": values[0], "end": values[-1], "return": total,
            "annual_return": annual, "volatility": sd * scale,
            "sharpe": mean / sd * scale if sd > 0 else None,
            "sortino": mean / down * scale if down > 0 else None,
            "calmar": annual / max_dd if max_dd > 0 else None,
            "max_drawdown": max_dd, "max_drawdown_days": dd_days}


def trade_distribution(pnls: list[float]) -> dict:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    streak = {"win": 0, "loss": 0}
    best_streak = {"win": 0, "loss": 0}
    for p in pnls:
        kind = "win" if p > 0 else "loss" if p < 0 else None
        for k in streak:
            streak[k] = streak[k] + 1 if k == kind else 0
            best_streak[k] = max(best_streak[k], streak[k])
    ordered = sorted(pnls)
    n = len(pnls)
    median = None
    if n:
        median = ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    return {"trades": n, "wins": len(wins), "losses": len(losses), "flat": n - len(wins) - len(losses),
            "win_rate": len(wins) / n if n else None, "net": sum(pnls),
            "expectancy": sum(pnls) / n if n else None, "median": median,
            "best": ordered[-1] if n else None, "worst": ordered[0] if n else None,
            "average_win": avg_win, "average_loss": avg_loss,
            "payoff": avg_win / -avg_loss if avg_win and avg_loss else None,
            "profit_factor": sum(wins) / -sum(losses) if losses else None,
            "max_consecutive_wins": best_streak["win"],
            "max_consecutive_losses": best_streak["loss"]}


def buy_and_hold(closes: dict, since: datetime, now: datetime, start_capital: float) -> dict | None:
    """Référence passive sur le même calendrier. `closes` : {date ISO (UTC): clôture}.
    Base = dernière clôture connue AVANT le premier jour ; sans elle, pas de référence."""
    first, last = _utc_day(since), _utc_day(now)
    prices: dict[date, float] = {}
    for key, price in closes.items():
        price = _number(price, f"clôture du {key}")
        if price <= 0:
            raise ValueError(f"clôture du {key} non positive")
        prices[date.fromisoformat(key)] = price
    before = [d for d in prices if d < first]
    if not before:
        return None
    base = prices[max(before)]
    values, price, day = [start_capital], base, first
    while day <= last:
        price = prices.get(day, price)
        values.append(start_capital * price / base)
        day += timedelta(days=1)
    return curve_metrics(values)


def build_report(name: str, start_capital: float, trades: list[tuple[datetime, float]],
                 since: datetime, now: datetime, closes: dict | None = None,
                 ignored_lines: int = 0) -> dict:
    """Rapport complet à partir de trades déjà validés (date avec fuseau, résultat en €)."""
    curve = daily_curve(start_capital, trades, since, now)
    metrics = curve_metrics([v for _, v in curve])
    notes = [NOTE_REALIZED]
    if metrics["days"] < SHORT_PERIOD_DAYS:
        notes.append(f"Période courte ({metrics['days']} jours) : chiffres annualisés peu fiables.")
    if ignored_lines:
        notes.append(f"{ignored_lines} ligne(s) du journal ignorée(s) (illisibles ou non-objets).")
    benchmark = None
    if closes is not None:
        benchmark = buy_and_hold(closes, since, now, start_capital)
        if benchmark is None:
            notes.append("Acheter-et-garder indisponible : aucune clôture avant le début.")
        else:
            notes.append(NOTE_MIXED)
    return {"name": name, "since": since.isoformat(), "now": now.isoformat(),
            "realized_only": True, "metrics": metrics,
            "trades": trade_distribution([p for _, p in trades]),
            "buy_and_hold": benchmark,
            "excess_return": metrics["return"] - benchmark["return"] if benchmark else None,
            "curve": curve, "ignored_lines": ignored_lines, "notes": notes}


def _lab_trades(fills: list[dict], since: datetime, now: datetime) -> list[tuple[datetime, float]]:
    trades = []
    for f in fills:
        if f.get("side") != "sell":
            continue
        moment = _aware(f.get("ts"), "date de vente")
        if since <= moment <= now:
            trades.append((moment, _number(f.get("realized_pnl"), "realized_pnl")))
    return sorted(trades, key=lambda t: t[0])


def lab_report(directory, name: str, start_capital: float, now: datetime,
               closes: dict | None = None) -> dict:
    """Rapport d'une stratégie du laboratoire, depuis `since.txt` et `<name>_ledger.jsonl`."""
    now = _aware(now, "now")
    since_file = Path(directory) / "since.txt"
    if not since_file.exists():
        raise ValueError("Début du laboratoire inconnu (since.txt absent).")
    since = _aware(since_file.read_text(encoding="utf-8").strip(), "début du laboratoire")
    fills, ignored = read_ledger(Path(directory) / f"{name}_ledger.jsonl")
    return build_report(name, start_capital, _lab_trades(fills, since, now), since, now,
                        closes, ignored)


def lab_reports(directory, names, start_capital: float, now: datetime,
                closes: dict | None = None) -> dict:
    """Un rapport par stratégie ; une stratégie aux données douteuses est refusée, pas devinée."""
    out = {}
    for name in names:
        try:
            out[name] = lab_report(directory, name, start_capital, now, closes)
        except ValueError as exc:
            out[name] = {"name": name, "error": str(exc)}
    return out


def shadow_report(path, start_capital: float, risk_per_trade: float, now: datetime,
                  closes: dict | None = None) -> dict:
    """Rapport du mode Shadow : chaque trade hypothétique clos risque `risk_per_trade` du
    capital initial (plafonné comme la configuration à 1 %), soit r × risque × capital en €.
    Le détail par protection utilise exactement les mêmes trades que la distribution."""
    now = _aware(now, "now")
    risk = _number(risk_per_trade, "risque par trade")
    if not 0 < risk <= MAX_RISK_PER_TRADE:
        raise ValueError(f"Risque par trade hors bornes (0 < risque ≤ {MAX_RISK_PER_TRADE:.0%}).")
    capital = _number(start_capital, "capital initial")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("closed", []), list):
        raise ValueError("Fichier Shadow illisible.")
    closed = data.get("closed", [])
    reference = data.get("reference")
    if isinstance(reference, dict) and reference.get("since"):
        since = _aware(reference["since"], "début de la référence")
    elif closed:
        since = min(_aware(t.get("closed_at"), "date de clôture") for t in closed if isinstance(t, dict))
    else:
        raise ValueError("Début du mode Shadow inconnu (ni référence ni trade clos).")
    trades, blockers = [], []
    for t in closed:
        if not isinstance(t, dict):
            raise ValueError("Trade Shadow qui n'est pas un objet.")
        moment = _aware(t.get("closed_at"), "date de clôture")
        if since <= moment <= now:
            trades.append((moment, _number(t.get("r"), "r") * risk * capital))
            blockers.append(str(t.get("blocked_by") or "inconnu"))
    order = sorted(range(len(trades)), key=lambda i: trades[i][0])
    trades = [trades[i] for i in order]
    blockers = [blockers[i] for i in order]
    report = build_report("shadow", capital, trades, since, now, closes)
    report["risk_per_trade"] = risk
    report["by_blocker"] = {b: trade_distribution([p for (_, p), x in zip(trades, blockers) if x == b])
                            for b in sorted(set(blockers))}
    return report


def render(report: dict) -> str:
    """Version texte courte d'un rapport."""
    if "error" in report:
        return f"{report['name']} : rapport refusé — {report['error']}"

    def pct(x):
        return "n/d" if x is None else f"{x:+.2%}"

    def num(x):
        return "n/d" if x is None else f"{x:.2f}"

    m, t = report["metrics"], report["trades"]
    lines = [f"== {report['name']} ({report['since'][:10]} → {report['now'][:10]}, {m['days']} jours) ==",
             f"Rendement {pct(m['return'])} (annualisé {pct(m['annual_return'])}), "
             f"volatilité {pct(m['volatility'])}",
             f"Sharpe {num(m['sharpe'])}  Sortino {num(m['sortino'])}  Calmar {num(m['calmar'])}",
             f"Perte maximale {pct(-m['max_drawdown'])} sur {m['max_drawdown_days']} jour(s)",
             f"Trades {t['trades']} (gagnants {t['wins']}, perdants {t['losses']}), "
             f"espérance {num(t['expectancy'])} €, profit factor {num(t['profit_factor'])}"]
    if report["buy_and_hold"]:
        lines.append(f"Acheter-et-garder {pct(report['buy_and_hold']['return'])}, "
                     f"écart {pct(report['excess_return'])}")
    lines += [f"! {n}" for n in report["notes"]]
    return "\n".join(lines)
