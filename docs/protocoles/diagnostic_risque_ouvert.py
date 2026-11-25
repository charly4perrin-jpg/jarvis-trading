"""Relevé jour par jour du risque ouvert (demandé par la relecture croisée). Lecture seule.

Un seul portefeuille réel : le backtest de portefeuille à capital partagé de volume_expansion
sur le panier officiel (c'est le cas qui ressemble à l'essai principal). On n'additionne
jamais des portefeuilles fictifs indépendants.

Règles de classement fixées AVANT de lire les données :
- jour de baisse commune : rendement moyen du panier (clôture à clôture) parmi les 5 % les
  plus mauvais des jours officiels ;
- risque ouvert élevé : risque jusqu'au stop des positions ouvertes et ordres en attente, à
  la clôture de la veille, au moins égal à la médiane des jours où ce risque est > 0 ;
- grappe de stops : au moins 2 stops déclenchés le même jour.
Pour chaque entrée : le plafond corrélé (1 % du capital) a-t-il refusé ou réduit l'ordre ?
"""
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, ".")
import trading_agent.portfolio_backtest as pb  # noqa: E402
from trading_agent.config import Settings  # noqa: E402
from trading_agent.evaluate_cli import load_histories  # noqa: E402
from trading_agent.exchanges import PublicExchange  # noqa: E402
from trading_agent.paper import PaperBroker  # noqa: E402
from trading_agent.risk import RiskManager  # noqa: E402

SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR", "LINK/EUR", "DOGE/EUR", "LTC/EUR"]
D = 86_400_000


def day(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


async def histories():
    async with PublicExchange("bitvavo") as ex:
        h = await load_histories(ex, SYMBOLS, "1d", 1825)
        return {k: v["candles"] for k, v in h.items()}, ex.timeframe_seconds("1d")


def main():
    hist, tf = asyncio.run(histories())
    settings = Settings()
    log = {"buys": [], "sells": [], "decisions": []}
    orig_buy, orig_close, orig_eval = PaperBroker.buy, PaperBroker.close_position, RiskManager.evaluate

    def buy(self, order, candle_ms=0, asks=None):
        r = orig_buy(self, order, candle_ms, asks)
        if r.get("status") == "filled":
            log["buys"].append({"symbol": order.symbol, "ts": candle_ms + 1, "qty": r["quantity"],
                                "price": r["fill_price"], "stop": order.stop_price})
        return r

    def close(self, symbol, reference_price, reason="manual", bids=None):
        r = orig_close(self, symbol, reference_price, reason, bids)
        if r.get("status") == "filled":
            log["sells"].append({"symbol": symbol, "pnl": r["realized_pnl"], "reason": reason})
        return r

    def evaluate(self, signal, portfolio, market=None):
        d = orig_eval(self, signal, portfolio, market)
        cor = (market or {}).get("correlated_open_risk")
        log["decisions"].append({"symbol": signal.symbol, "approved": d.approved, "reason": d.reason,
                                 "correlated_open_risk": cor, "equity": portfolio.equity,
                                 "reduced": bool(cor is not None and d.approved and
                                                 portfolio.equity * self.s.max_correlated_risk_pct - cor
                                                 < portfolio.equity * self.s.risk_per_trade_pct)})
        return d

    PaperBroker.buy, PaperBroker.close_position, RiskManager.evaluate = buy, close, evaluate
    try:
        r = pb.run_portfolio(hist, settings, tf, ["volume_expansion"])
    finally:
        PaperBroker.buy, PaperBroker.close_position, RiskManager.evaluate = orig_buy, orig_close, orig_eval

    # Dates des ventes, depuis le rapport (les fills portent l'horodatage de la bougie).
    sells = [f for f in r["fills"] if f["side"] == "sell"]
    buys = [f for f in r["fills"] if f["side"] == "buy"]
    stops_by_day = defaultdict(list)
    for f in sells:
        if f.get("reason") == "stop":
            stops_by_day[f["ts_ms"]].append((f["symbol"], f["realized_pnl"]))
    # Positions ouvertes : appariement achat → vente par paire, dans l'ordre.
    stops = {}
    for b, lb in zip(buys, log["buys"]):
        stops[(b["symbol"], b["ts_ms"])] = lb["stop"]
    spans = []
    for s in SYMBOLS:
        bs = [b for b in buys if b["symbol"] == s]
        ss = [x for x in sells if x["symbol"] == s]
        for b, x in zip(bs, ss + [None] * (len(bs) - len(ss))):
            spans.append({"symbol": s, "from": b["ts_ms"], "to": x["ts_ms"] if x else None,
                          "qty": b["quantity"], "stop": stops[(s, b["ts_ms"])]})
    closes = {s: {c[0]: float(c[4]) for c in hist[s]} for s in SYMBOLS}
    timeline = sorted(set.intersection(*(set(closes[s]) for s in SYMBOLS)))
    rows = []
    for prev, ts in zip(timeline, timeline[1:]):
        basket = statistics.mean(closes[s][ts] / closes[s][prev] - 1 for s in SYMBOLS)
        risk = sum(p["qty"] * max(0.0, closes[p["symbol"]][prev] - p["stop"]) for p in spans
                   if p["from"] <= prev and (p["to"] is None or p["to"] >= ts))
        rows.append({"day": day(ts), "ts": ts, "btc": closes["BTC/EUR"][ts] / closes["BTC/EUR"][prev] - 1,
                     "basket": basket, "open_risk": risk,
                     "entries": sum(1 for b in buys if b["ts_ms"] == ts),
                     "stops": stops_by_day.get(ts, [])})
    cut = sorted(x["basket"] for x in rows)[int(0.05 * len(rows))]
    positive = [x["open_risk"] for x in rows if x["open_risk"] > 0]
    median = statistics.median(positive) if positive else 0.0
    for x in rows:
        x["common_drop"] = x["basket"] <= cut
        x["high_risk"] = x["open_risk"] > 0 and x["open_risk"] >= median
        x["cluster"] = len(x["stops"]) >= 2

    def summarize(sel):
        sel = list(sel)
        return {"jours": len(sel), "jours_avec_stop": sum(1 for x in sel if x["stops"]),
                "stops": sum(len(x["stops"]) for x in sel),
                "perte_stops": round(sum(p for x in sel for _, p in x["stops"]), 2),
                "grappes": sum(1 for x in sel if x["cluster"])}

    decisions = log["decisions"]
    out = {
        "seuils": {"baisse_commune_panier": round(cut, 4), "risque_eleve_eur": round(median, 2)},
        "baisse_risque_eleve": summarize(x for x in rows if x["common_drop"] and x["high_risk"]),
        "baisse_risque_faible": summarize(x for x in rows if x["common_drop"] and not x["high_risk"]),
        "sans_baisse_risque_eleve": summarize(x for x in rows if not x["common_drop"] and x["high_risk"]),
        "tous_les_jours": summarize(rows),
        "grappes": [{"day": x["day"], "panier": round(x["basket"], 4), "btc": round(x["btc"], 4),
                     "risque_ouvert": round(x["open_risk"], 2), "stops": x["stops"],
                     "baisse_commune": x["common_drop"]} for x in rows if x["cluster"]],
        "plafond_correle": {"decisions": len(decisions),
                            "refus": sum(1 for d in decisions if d["reason"] == "Correlated risk limit reached."),
                            "reductions": sum(1 for d in decisions if d["reduced"]),
                            "autres_refus": dict(sorted({d["reason"]: sum(1 for e in decisions if e["reason"] == d["reason"])
                                                         for d in decisions if not d["approved"]}.items()))},
        "portefeuille": {"return": r["return"], "trades": r["trades"], "max_drawdown": r["max_drawdown"]},
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
