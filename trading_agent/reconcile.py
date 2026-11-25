"""Réconciliation papier ↔ backtest, lot 1 : les DÉCISIONS, cycle par cycle.

Pour chaque cycle enregistré dans le journal (bus d'événements), on recharge les mêmes
bougies closes depuis la plateforme, on refait passer le même code de décision (détecteur
de régime et analyste de stratégie, réglages identiques) et on cherche le PREMIER endroit
où le rejeu diverge de ce que l'essai a fait.

Statuts, par cycle :
- « conforme » : mêmes données et même décision (régime, lecture de Markov, signal ou
  absence de signal, veto et motif) ;
- « divergence » : mêmes données, mais une étape décide autrement (laquelle est indiquée) ;
- « non vérifiable » : impossible de garantir que les données rejouées sont celles de
  l'essai (bougies manquantes, prix différent, empreinte différente) ou cycle sans décision.
  Jamais présenté comme conforme.

Exécutions (lot 2) : l'essai enregistre le carnet exact qu'il consomme (même lecture, pas
de deuxième requête) et les paramètres du calcul. Le rejeu reconsomme CE carnet avec la même
quantité, la même limite de glissement et le même taux de frais, et compare quantité, prix
et frais. Sans carnet enregistré (anciens cycles), ou pour les exécutions partielles
ultérieures d'un ordre limite (un seul instantané ne suffit pas), le résultat est « non
vérifiable » : on ne reconstruit jamais un fill à partir des bougies ni du prix d'ouverture.
Lecture seule : rien n'est écrit, aucun ordre.
"""
import hashlib
import json
from collections import defaultdict

DECISIONS = ("strategy.signal", "strategy.no_signal", "strategy.exit_signal")


def fingerprint(candles: list) -> str:
    """Empreinte exacte des bougies utilisées pour décider (enregistrée par l'essai)."""
    return hashlib.sha256(json.dumps(candles, separators=(",", ":")).encode()).hexdigest()


def _kind(event) -> str:
    p = event.payload or {}
    if event.type == "strategy.signal":
        return "signal"
    if event.type == "strategy.exit_signal":
        return "sortie"
    if event.result.startswith("Position conservée"):
        return "position conservée"
    if p.get("paused"):
        return "écarté : pause"
    if p.get("hmm_blocked"):
        return "écarté : Markov"
    if p.get("regime_blocked"):
        return "écarté : régime"
    return "aucun signal"


def cycles(events) -> list[dict]:
    """Cycles de décision enregistrés : un instantané de marché suivi d'une décision."""
    by_corr = defaultdict(list)
    for e in events:
        by_corr[e.correlation_id].append(e)
    out = []
    for corr, evts in by_corr.items():
        snap = next((e for e in evts if e.type == "market.snapshot"), None)
        if snap is None:
            continue
        regime = next((e for e in evts if e.type == "regime.detected"), None)
        decision = next((e for e in evts if e.type in DECISIONS), None)
        books = [e for e in evts if e.type == "market.order_book"]
        fills = [e.payload for e in evts if e.type == "execution.fill"]
        out.append({"corr": corr, "ts": snap.ts.isoformat(), "snapshot": snap.payload,
                    "book": books[-1].payload if books else None, "fills": fills,
                    "regime": regime.payload if regime else None,
                    "decision": {"kind": _kind(decision), "strategy": (decision.payload or {}).get("strategy"),
                                 "text": decision.result} if decision else None})
    return sorted(out, key=lambda c: c["ts"])


async def fetch_same_candles(ex, snap: dict) -> tuple[list | None, str]:
    """Recharge les bougies closes de l'instantané. Renvoie (bougies, raison si refus)."""
    tf_ms = ex.timeframe_seconds(snap["timeframe"]) * 1000
    count, last = int(snap["candle_count"]), int(snap["last_candle_ms"])
    first = int(snap.get("first_candle_ms") or last - (count - 1) * tf_ms)
    raw = await ex.ohlcv(snap["symbol"], timeframe=snap["timeframe"], limit=count + 5, since=first)
    candles = [c for c in raw or [] if first <= c[0] <= last]
    if len(candles) != count:
        return None, f"{len(candles)} bougies rechargées au lieu de {count}"
    if abs(float(candles[-1][4]) - float(snap["last_price"])) > 1e-12 * max(1.0, float(snap["last_price"])):
        return None, "dernière clôture rechargée différente de celle de l'essai"
    if snap.get("candles_sha256") and fingerprint(candles) != snap["candles_sha256"]:
        return None, "empreinte des bougies différente : la plateforme a révisé l'historique"
    return candles, ""


def replay(candles: list, snap: dict, decision: dict, settings) -> dict:
    """Refait passer le code de décision de l'essai sur ces bougies, sans rien écrire."""
    from trading_agent.agents import MarketSnapshot, RegimeDetector, StrategyAnalyst
    from trading_agent.events import EventBus
    bus = EventBus(None)
    s = MarketSnapshot(snap["exchange"], snap["symbol"], snap["timeframe"], candles,
                       float(snap["last_price"]), snap.get("spread_bps"))
    RegimeDetector(bus).detect(s, "rejeu")
    holding = decision["kind"] in ("position conservée", "sortie")
    analyst = StrategyAnalyst(bus, decision.get("strategy") or settings.strategy, settings.regime_filter,
                              None, settings.learning_dir, settings.hmm_filter)
    analyst.analyze(s, holding, "rejeu", decision.get("strategy") if holding else None)
    ev = next(e for e in reversed(bus.events) if e.type in DECISIONS)
    return {"regime": s.regime, "hmm": s.hmm, "kind": _kind(ev), "text": ev.result}


def replay_execution(book: dict | None, fill: dict) -> tuple[str, str]:
    """Reconsomme le carnet enregistré avec les paramètres exacts de l'essai."""
    from trading_agent.paper import walk_book
    if fill.get("order_type") == "limit":
        return "non vérifiable", "ordre limite : ses exécutions partielles ultérieures ne sont pas dans l'instantané"
    if not fill.get("used_book") or "reference_price" not in fill:
        return "non vérifiable", "exécution enregistrée sans ses paramètres de calcul"
    side = fill.get("side")
    levels = (book or {}).get("asks" if side == "buy" else "bids")
    if not levels:
        return "non vérifiable", "carnet d'ordres non enregistré pour ce cycle"
    buying = side == "buy"
    ref, slip = float(fill["reference_price"]), float(fill["max_slippage"])
    limit = ref * (1 + slip) if buying else ref * (1 - slip)
    qty, price = walk_book(levels, float(fill["requested_quantity"]), limit, buying=buying)
    fee = price * qty * float(fill["fee_rate"])
    if qty != fill["quantity"]:
        return "divergence", f"quantité : essai {fill['quantity']}, rejeu {qty}"
    if price != fill["fill_price"]:
        return "divergence", f"prix : essai {fill['fill_price']}, rejeu {price}"
    if abs(fee - fill["fee"]) > 1e-9 * max(1.0, abs(fill["fee"])):
        return "divergence", f"frais : essai {fill['fee']}, rejeu {fee}"
    return "conforme", f"{'achat' if buying else 'vente'} rejoué sur le carnet enregistré"


def compare(recorded: dict, replayed: dict) -> tuple[str, str]:
    """Premier écart, dans l'ordre du parcours de décision."""
    reg = recorded["regime"] or {}
    if reg.get("regime") != replayed["regime"]:
        return "divergence", "régime de marché"
    if "hmm" in reg and reg.get("hmm") != replayed["hmm"]:
        return "divergence", "lecture de Markov"
    rec = recorded["decision"]
    if recorded["decision"]["kind"] == "écarté : pause":
        # Les pauses dépendent de l'état du moment, pas des bougies : on compare le signal brut.
        return ("conforme", "signal présent (stratégie en pause à ce moment-là)") \
            if replayed["kind"] != "aucun signal" else ("divergence", "signal de la stratégie")
    if rec["kind"] != replayed["kind"]:
        return "divergence", f"décision : essai « {rec['kind']} », rejeu « {replayed['kind']} »"
    if rec["text"] != replayed["text"]:
        return "divergence", "même décision mais motif différent"
    return "conforme", rec["kind"]


async def reconcile(events, exchange_factory, settings, last: int = 20) -> dict:
    rows = []
    every = cycles(events)
    # Seuls les cycles qui ont décidé quelque chose se rejouent ; les autres (bougie déjà
    # analysée, redémarrage) sont comptés à part, sans être présentés comme conformes.
    decided = [c for c in every if c["decision"] is not None]
    selected = decided[-last:] if last else decided
    exchanges = {}
    try:
        for c in selected:
            execs = [replay_execution(c["book"], f) for f in c["fills"]]
            row = {"ts": c["ts"], "corr": c["corr"], "exchange": c["snapshot"].get("exchange"),
                   "symbol": c["snapshot"].get("symbol"),
                   "execution": " ; ".join(f"{st} — {d}" for st, d in execs) if execs else "aucune exécution"}
            if c["decision"] is None:
                rows.append({**row, "status": "non vérifiable", "detail": "aucune décision dans ce cycle"})
                continue
            try:
                ex_id = c["snapshot"]["exchange"]
                if ex_id not in exchanges:
                    exchanges[ex_id] = await exchange_factory(ex_id).__aenter__()
                candles, why = await fetch_same_candles(exchanges[ex_id], c["snapshot"])
                if candles is None:
                    rows.append({**row, "status": "non vérifiable", "detail": why})
                    continue
                status, detail = compare(c, replay(candles, c["snapshot"], c["decision"], settings))
                if not c["snapshot"].get("candles_sha256"):   # cycles enregistrés avant l'empreinte
                    detail += " (données vérifiées par nombre de bougies et dernière clôture, sans empreinte)"
                rows.append({**row, "status": status, "detail": detail})
            except Exception as exc:   # une erreur n'est jamais « conforme »
                rows.append({**row, "status": "non vérifiable", "detail": f"{type(exc).__name__}: {exc}"})
    finally:
        for ex in exchanges.values():
            await ex.__aexit__(None, None, None)
    counts = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    return {"cycles": len(rows), "counts": dict(counts), "rows": rows,
            "without_decision": len(every) - len(decided)}


async def main(args) -> None:
    from trading_agent.config import Settings
    from trading_agent.events import read_events
    from trading_agent.exchanges import PublicExchange
    settings = Settings()
    report = await reconcile(read_events(settings.event_log_path), PublicExchange, settings, args.last)
    print(f"RÉCONCILIATION PAPIER ↔ BACKTEST (décisions) : {report['cycles']} cycles rejoués "
          f"({report['without_decision']} cycles sans décision ignorés : bougie déjà analysée)")
    for status in ("conforme", "divergence", "non vérifiable"):
        print(f"  {status} : {report['counts'].get(status, 0)}")
    for r in report["rows"]:
        if r["status"] != "conforme" or args.all:
            print(f"  {r['ts'][:19]} {r['exchange']} {r['symbol']} : {r['status']} — {r['detail']}")
    execs = [r for r in report["rows"] if r["execution"] != "aucune exécution"]
    print(f"Exécutions rejouées : {len(execs)}")
    for r in execs:
        print(f"  {r['ts'][:19]} {r['exchange']} {r['symbol']} : {r['execution']}")
