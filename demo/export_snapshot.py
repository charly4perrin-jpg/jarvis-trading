"""Exporte un instantané PUBLIC et nettoyé de Jarvis pour la démo en ligne.

    .venv/bin/python -m demo.export_snapshot            # écrit demo/site/snapshot.json
    .venv/bin/python -m demo.export_snapshot --out x.json

Principe : liste blanche. Seuls les types d'événements et les champs nommés ici sortent.
Rien de la configuration, de l'IA, des notifications, du fichier .env ni des réglages de risque.
Lecture seule : le script ne modifie aucune donnée de l'essai. Bibliothèque standard uniquement.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
DATA = RACINE / "data"
SORTIE = Path(__file__).resolve().parent / "site" / "snapshot.json"

TYPES_FIL = {
    "cycle.end", "regime.detected", "market.regime", "strategy.signal", "strategy.no_signal",
    "strategy.exit_signal", "adversarial.review", "trade.proposal", "allocation.assessment",
    "risk.decision", "routing.plan", "execution.order_placed", "execution.fill", "execution.rejected",
    "execution.expired", "execution.cancelled", "exchange.status", "performance.report",
    "performance.degradation", "system.halt", "system.resume", "arbitrage.scan", "news.update",
}
NOMS = {
    "coordinator": "Coordinateur", "market_intelligence": "Marché", "regime_detector": "Régime",
    "strategy_analyst": "Stratégie", "adversarial_analyst": "Contradicteur", "trade_manager": "Proposition",
    "portfolio_optimizer": "Allocation", "risk_manager": "Risque", "execution_intelligence": "Routage",
    "execution_agent": "Exécution", "portfolio_manager": "Portefeuille", "exchange_monitor": "Plateformes",
    "performance_analyst": "Performance", "arbitrage_analyst": "Arbitrage", "learning_manager": "Apprentissage",
    "news_analyst": "Actualités", "security_monitor": "Sécurité", "auditor": "Audit",
}
MASQUES = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[e-mail masqué]"),
    (re.compile(r"(/Users|/home|C:\\Users)[^\s'\"]*"), "[chemin masqué]"),
    (re.compile(r"\b(sk|xai|pk)-[A-Za-z0-9_-]{8,}"), "[clé masquée]"),
    (re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*\S+"), r"\1=[masqué]"),
    (re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"), "[IP masquée]"),
]


def propre(texte, n=280):
    t = str(texte or "")
    for rx, rep in MASQUES:
        t = rx.sub(rep, t)
    return t[:n]


def nombre(x):
    return round(x, 4) if isinstance(x, (int, float)) and x == x and abs(x) != float("inf") else None


def lire_evenements(chemin):
    if not chemin.exists():
        return []
    out = []
    with open(chemin, encoding="utf-8") as f:
        for ligne in f:
            try:
                out.append(json.loads(ligne))
            except json.JSONDecodeError:
                continue
    return out


def portefeuilles(perf):
    res = []
    for ex in (perf or {}).get("exchanges", []):
        tr, cv = ex.get("trades") or {}, ex.get("curve") or {}
        res.append({
            "plateforme": propre(ex.get("exchange"), 20),
            "capital": nombre(ex.get("equity")), "depart": nombre(ex.get("start")),
            "rendement_net": nombre(ex.get("net_return")), "frais": nombre(ex.get("fees")),
            "trades": tr.get("trades"), "gagnants": tr.get("wins"),
            "perte_max": nombre(cv.get("max_drawdown")), "sharpe": nombre(cv.get("sharpe")),
        })
    return res


def construire(nb_fil=80):
    ev = lire_evenements(DATA / "events.jsonl")
    maintenant = dt.datetime.now(dt.timezone.utc)
    il_y_a_24h = (maintenant - dt.timedelta(hours=24)).isoformat()
    il_y_a_7j = (maintenant - dt.timedelta(days=7)).isoformat()

    perf = next((e for e in reversed(ev) if e.get("type") == "performance.report"), None)
    regimes = {}
    for e in ev:
        if e.get("type") == "regime.detected":
            p = e.get("payload") or {}
            r = p.get("regime") or {}
            sym = propre(p.get("symbol"), 20)
            regimes[sym] = {"tendance": propre(r.get("trend"), 30), "volatilite": propre(r.get("volatility"), 30),
                            "choc": bool(r.get("shock")), "le": e.get("ts", "")[:16]}
    statuts = {}
    for e in ev:
        if e.get("type") == "exchange.status":
            p = e.get("payload") or {}
            statuts[propre(p.get("exchange"), 20)] = {"etat": propre(p.get("status"), 20),
                                                      "latence_ms": nombre((p.get("health") or {}).get("latency_ms")),
                                                      "le": e.get("ts", "")[:16]}
    activite = collections.Counter(NOMS.get(e.get("source"), "Autre") for e in ev if e.get("ts", "") >= il_y_a_24h)
    semaine = [e for e in ev if e.get("ts", "") >= il_y_a_7j]
    compte = collections.Counter(e.get("type") for e in semaine)
    refus = collections.Counter(NOMS.get(e.get("source"), "Autre") for e in semaine
                                if e.get("type") in ("adversarial.review", "risk.decision", "execution.rejected")
                                and e.get("status") not in ("ok", "approved", "accepted"))
    fil = [{
        "le": e.get("ts", "")[:19], "de": NOMS.get(e.get("source"), "Autre"), "vers": NOMS.get(e.get("target"), ""),
        "type": e.get("type"), "etat": propre(e.get("status"), 20), "message": propre(e.get("result")),
    } for e in ev if e.get("type") in TYPES_FIL][-nb_fil:]
    dernier = ev[-1] if ev else {}
    return {
        "genere_le": maintenant.isoformat(timespec="seconds"),
        "avertissement": "Projet personnel en argent fictif (paper trading). Aucun ordre réel. Pas un conseil en investissement.",
        "dernier_evenement": dernier.get("ts", "")[:19],
        "journal": {"evenements": len(ev), "empreinte_finale": str(dernier.get("hash", ""))[:16]},
        "portefeuilles": portefeuilles((perf or {}).get("payload")),
        "shadow": propre((perf or {}).get("result"), 400) if perf else "",
        "regimes": regimes, "plateformes": statuts,
        "activite_24h": dict(activite.most_common()),
        "semaine": {"cycles": compte.get("cycle.end", 0), "signaux": compte.get("strategy.signal", 0),
                    "propositions": compte.get("trade.proposal", 0), "executions": compte.get("execution.fill", 0),
                    "arrets_urgence": compte.get("system.halt", 0), "refus_par_agent": dict(refus)},
        "fil": fil,
    }


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--out", default=str(SORTIE))
    a = a.parse_args()
    s = construire()
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(a.out).with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(a.out)
    print(f"Instantané écrit : {a.out} ({len(s['fil'])} messages, {s['journal']['evenements']} événements)")


if __name__ == "__main__":
    main()
