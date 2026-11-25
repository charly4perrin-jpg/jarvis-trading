"""Diagnostic des fenêtres de l'évaluation officielle (demandé par la relecture croisée).
Lecture seule : ne modifie ni l'évaluation officielle ni ses résultats.

1. Fenêtre 1 : même évaluation, mais les indicateurs reçoivent l'historique ANTÉRIEUR
   disponible (400 jours de plus), sans ouvrir de position ni compter de performance
   avant le début officiel.
2. Frontières : un trade réel qui traverse une frontière de fenêtre, suivi dans le rapport.
3. Tableau stratégie × fenêtre : rendement, trades, signaux bruts, vetos, pertes.
"""
import asyncio
import json
import math
import sys
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

sys.path.insert(0, ".")
from trading_agent.backtest import run_backtest  # noqa: E402
from trading_agent.config import Settings  # noqa: E402
from trading_agent.evaluate_cli import load_histories  # noqa: E402
from trading_agent.exchanges import PublicExchange  # noqa: E402
from trading_agent.strategies import get_strategy  # noqa: E402

SYMBOLS = ["BTC/EUR", "ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR", "LINK/EUR", "DOGE/EUR", "LTC/EUR"]
STRATS = ["volume_expansion", "relative_strength", "compression_breakout", "dip_rebound"]
DAYS, EXTRA, WINDOWS = 1825, 400, 6


def day(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def gated(strat, start_ts):
    """Aucune entrée avant le début officiel : les bougies antérieures ne servent qu'aux indicateurs."""
    entry = strat.entry
    return replace(strat, entry=lambda symbol, candles: None if candles[-1][0] < start_ts else entry(symbol, candles))


def windows_of(report, candles, start_idx):
    """Même découpage que l'évaluation officielle, sur la partie officielle de la série."""
    curve = report["equity_curve"][start_idx:]
    official = candles[start_idx:]
    n = len(official)
    bounds = [round(i * n / WINDOWS) for i in range(WINDOWS + 1)]
    sells = [f for f in report["fills"] if f["side"] == "sell"]
    out = []
    for a, b in zip(bounds, bounds[1:]):
        start = curve[a - 1] if a else report["start_equity"]
        end_ts = official[b][0] if b < n else math.inf
        ws = [f for f in sells if official[a][0] <= f["ts_ms"] < end_ts]
        out.append({"from": day(official[a][0]), "to": day(official[b - 1][0]),
                    "return": curve[b - 1] / start - 1, "trades": len(ws),
                    "pnl": sum(f["realized_pnl"] for f in ws), "fees": sum(f["fee"] + f.get("entry_fee", 0) for f in ws),
                    "losses": sorted(((f["realized_pnl"], day(f["ts_ms"]), f.get("reason")) for f in ws
                                      if f["realized_pnl"] < 0))[:3]})
    return out, bounds, official


async def histories(days):
    async with PublicExchange("bitvavo") as ex:
        h = await load_histories(ex, SYMBOLS, "1d", days)
        return h, ex.timeframe_seconds("1d")


def main():
    settings = Settings()
    official, tf = asyncio.run(histories(DAYS))
    extended, _ = asyncio.run(histories(DAYS + EXTRA))
    out = {"available_from": {s: day(extended[s]["candles"][0][0]) for s in SYMBOLS},
           "official_from": {s: day(official[s]["candles"][0][0]) for s in SYMBOLS}, "strategies": {}}
    for name in STRATS:
        base = get_strategy(name)
        res = {"official": [], "warm": [], "signals": [], "vetos": Counter(), "first_signal": {},
               "crossing": None}
        bound_off = base.universe({s: h["candles"] for s, h in official.items()}) if base.universe else base
        bound_ext = base.universe({s: h["candles"] for s, h in extended.items()}) if base.universe else base
        for s in SYMBOLS:
            oc, ec = official[s]["candles"], extended[s]["candles"]
            local = settings.model_copy(update={"paper_fee_rate": official[s].get("fee") or settings.paper_fee_rate})
            r_off = run_backtest(oc, local, s, tf, official[s]["limits"], strategy=bound_off)
            w_off, bounds, _ = windows_of(r_off, oc, 0)
            start_idx = next(i for i, c in enumerate(ec) if c[0] >= oc[0][0])
            r_warm = run_backtest(ec, local, s, tf, official[s]["limits"], strategy=gated(bound_ext, oc[0][0]))
            w_warm, _, _ = windows_of(r_warm, ec, start_idx)
            res["official"].append(w_off)
            res["warm"].append(w_warm)
            res["vetos"].update(r_off.get("refusals", {}))
            # Premier jour où la stratégie peut réellement émettre un signal (hors filtre de régime).
            first = next((day(oc[i][0]) for i in range(len(oc))
                          if bound_off.entry(s, oc[max(0, i - 299): i + 1])), None)
            res["first_signal"][s] = first
            # Un trade qui traverse une frontière de fenêtre (achat avant, vente après).
            if res["crossing"] is None:
                edges = [oc[b][0] for b in bounds[1:-1]]
                buys = [f for f in r_off["fills"] if f["side"] == "buy"]
                sells = [f for f in r_off["fills"] if f["side"] == "sell"]
                for bu, se in zip(buys, sells):
                    if any(bu["ts_ms"] < e <= se["ts_ms"] for e in edges):
                        res["crossing"] = {"symbol": s, "buy": day(bu["ts_ms"]), "buy_price": bu["fill_price"],
                                           "buy_fee": bu["fee"], "sell": day(se["ts_ms"]),
                                           "sell_price": se["fill_price"], "sell_fee": se["fee"],
                                           "pnl": se["realized_pnl"], "reason": se.get("reason"),
                                           "frontiere": next(day(e) for e in edges if bu["ts_ms"] < e <= se["ts_ms"])}
                        break
        summary = {}
        for label in ("official", "warm"):
            rows = res[label]
            summary[label] = [{"from": rows[0][k]["from"], "to": rows[0][k]["to"],
                               "return": sum(r[k]["return"] for r in rows) / len(rows),
                               "trades": sum(r[k]["trades"] for r in rows),
                               "pnl": sum(r[k]["pnl"] for r in rows), "fees": sum(r[k]["fees"] for r in rows),
                               "worst": sorted((x + (sym,) for r, sym in zip(rows, SYMBOLS) for x in r[k]["losses"]))[:3]}
                              for k in range(WINDOWS)]
        out["strategies"][name] = {"windows": summary, "vetos": dict(res["vetos"].most_common(6)),
                                   "first_signal": res["first_signal"], "crossing": res["crossing"]}
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
