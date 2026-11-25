"""Audit du plafond corrélé dans le portefeuille partagé (relecture croisée). Hors ligne :
aucun changement du moteur de risque ni de l'essai. volume_expansion, panier officiel, 5 ans.

1. Relevé des interventions du plafond sur le rejeu de référence : quantité demandée (sans
   plafond), quantité accordée, motif exact.
2. Quatre rejeux complets du portefeuille, mêmes données, frais, exécution et ordre de
   traitement ; seuls changent :
   - la base de dimensionnement de la NOUVELLE position : capital courant (comme aujourd'hui)
     ou capital initial (3 000 €) — le cash réel et les autres garde-fous restent ceux du
     moment ;
   - le plafond corrélé : en vigueur, ou retiré (comparateur contrefactuel).
   Approximation déclarée pour la base « capital initial » : la quantité est le minimum de la
   quantité permise par le plafond (calculée comme aujourd'hui) et de la quantité donnée par
   0,5 % de 3 000 €.
"""
import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone

sys.path.insert(0, ".")
import trading_agent.portfolio_backtest as pb  # noqa: E402
from trading_agent.config import Settings  # noqa: E402
from trading_agent.evaluate_cli import load_histories  # noqa: E402
from trading_agent.exchanges import PublicExchange  # noqa: E402
from trading_agent.risk import RiskManager  # noqa: E402
from trading_agent.strategies import STRATEGIES  # noqa: E402

SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR", "LINK/EUR", "DOGE/EUR", "LTC/EUR"]
CAP_REASON = "Correlated risk limit reached."


def day(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


async def histories():
    async with PublicExchange("bitvavo") as ex:
        h = await load_histories(ex, SYMBOLS, "1d", 1825)
        return {k: v["candles"] for k, v in h.items()}, ex.timeframe_seconds("1d")


def run(hist, tf, settings, cap: bool, fixed_base: bool, log: list | None = None):
    orig_eval = RiskManager.evaluate
    current = {"ts": None}
    strat = STRATEGIES["volume_expansion"]
    entry = strat.entry

    def tracked(symbol, candles):
        current["ts"] = candles[-1][0]
        return entry(symbol, candles)

    def evaluate(self, signal, portfolio, market=None):
        free = {k: v for k, v in (market or {}).items() if k != "correlated_open_risk"}
        uncapped = orig_eval(self, signal, portfolio, free)
        capped = orig_eval(self, signal, portfolio, market)
        decision = capped if cap else uncapped
        if decision.approved and fixed_base and uncapped.approved:
            scale = settings.paper_starting_cash / portfolio.equity
            q = uncapped.order.quantity * scale
            if cap:
                q = min(q, capped.order.quantity)
            decision = replace(decision, order=replace(decision.order, quantity=q))
        if log is not None and cap and not fixed_base:
            log.append({"day": day(current["ts"]), "symbol": signal.symbol,
                        "requested": uncapped.order.quantity if uncapped.approved else None,
                        "granted": capped.order.quantity if capped.approved else None,
                        "reason": capped.reason, "uncapped_reason": uncapped.reason})
        return decision

    RiskManager.evaluate = evaluate
    STRATEGIES["volume_expansion"] = replace(strat, entry=tracked)
    try:
        return pb.run_portfolio(hist, settings, tf, ["volume_expansion"])
    finally:
        RiskManager.evaluate = orig_eval
        STRATEGIES["volume_expansion"] = strat


def main():
    hist, tf = asyncio.run(histories())
    settings = Settings()
    log: list = []
    results = {}
    for name, cap, fixed in (("courant_plafond", True, False), ("courant_sans_plafond", False, False),
                             ("initial_plafond", True, True), ("initial_sans_plafond", False, True)):
        r = run(hist, tf, settings, cap, fixed, log if name == "courant_plafond" else None)
        results[name] = {"return": round(r["return"], 4), "max_drawdown": round(r["max_drawdown"], 4),
                         "trades": r["trades"], "expectancy": round(r["expectancy"] or 0, 2),
                         "windows": [round(w, 4) for w in r["window_returns"]], "refusals": r["refusals"]}
    refus = [x for x in log if x["reason"] == CAP_REASON]
    reduits = [x for x in log if x["granted"] is not None and x["requested"] is not None
               and x["granted"] < x["requested"] * (1 - 1e-9)]
    print(json.dumps({
        "interventions": {"refus_plafond": len(refus), "reductions": len(reduits),
                          "refus_qui_auraient_ete_acceptes_sans_plafond": sum(1 for x in refus if x["requested"]),
                          "detail_reductions": [{k: x[k] for k in ("day", "symbol", "requested", "granted")} for x in reduits],
                          "autres_refus": sorted({x["reason"] for x in log if x["granted"] is None and x["reason"] != CAP_REASON}),
                          "decisions": len(log)},
        "rejeux": results}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
