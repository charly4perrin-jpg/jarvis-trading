"""Règle de décision fixée par Luna (ChatGPT) avant le calcul de cette mesure :
portefeuille à capital partagé, témoin (veto baissier) contre variante (sans veto), mêmes
frais, risque et exécution ; écart de rendement net cumulé ; intervalle à 95 % par
rééchantillonnage de blocs de 28 jours, mêmes blocs pour les deux versions."""
import asyncio
import json
import random
import sys

sys.path.insert(0, ".")
from trading_agent.config import Settings  # noqa: E402
from trading_agent.evaluate_cli import load_histories  # noqa: E402
from trading_agent.exchanges import PublicExchange  # noqa: E402
from trading_agent.portfolio_backtest import run_portfolio  # noqa: E402
from trading_agent.regimes import regime_series  # noqa: E402

SYMBOLS = ["DOT/EUR", "AVAX/EUR", "ATOM/EUR", "BCH/EUR", "XLM/EUR", "ETC/EUR", "UNI/EUR", "AAVE/EUR"]
BLOCK, RESAMPLES, SEED = 28, 10_000, 20261122


async def histories():
    async with PublicExchange("bitvavo") as ex:
        h = await load_histories(ex, SYMBOLS, "1d", 1825)
        return {k: v["candles"] for k, v in h.items()}, ex.timeframe_seconds("1d")


def daily_returns(curve):
    return [b / a - 1 for a, b in zip(curve, curve[1:])]


def cumulative(rets):
    v = 1.0
    for r in rets:
        v *= 1 + r
    return v - 1


def main():
    hist, tf = asyncio.run(histories())
    settings = Settings()
    witness = run_portfolio(hist, settings, tf, ["volume_expansion"])
    variant = run_portfolio(hist, settings, tf, ["volume_expansion_bear"])
    rw, rv = daily_returns(witness["equity_curve"]), daily_returns(variant["equity_curve"])
    assert len(rw) == len(rv)
    observed = cumulative(rv) - cumulative(rw)

    # Trades supplémentaires : achats de la variante dont la bougie du signal était baissière.
    regimes = {s: regime_series(c) for s, c in hist.items()}
    index = {s: {c[0]: i for i, c in enumerate(cs)} for s, cs in hist.items()}
    sells = {(f["symbol"], f["ts_ms"]) for f in variant["fills"] if f["side"] == "sell"}
    extra = 0
    for f in variant["fills"]:
        if f["side"] != "buy":
            continue
        i = index[f["symbol"]].get(f["ts_ms"])
        reg = regimes[f["symbol"]][i - 1] if i else None
        if reg and reg["trend"] == "baissier":
            extra += 1
    closed_variant = len([f for f in variant["fills"] if f["side"] == "sell"])

    rng = random.Random(SEED)
    n = len(rw)
    diffs = []
    for _ in range(RESAMPLES):
        idx = []
        while len(idx) < n:
            start = rng.randrange(0, n - BLOCK + 1)
            idx.extend(range(start, start + BLOCK))
        idx = idx[:n]
        diffs.append(cumulative([rv[i] for i in idx]) - cumulative([rw[i] for i in idx]))
    diffs.sort()
    low, high = diffs[int(0.025 * RESAMPLES)], diffs[int(0.975 * RESAMPLES) - 1]

    out = {
        "periode": [str(witness["from"]), str(witness["to"])], "jours": n,
        "temoin": {k: witness[k] for k in ("return", "max_drawdown", "trades", "expectancy", "window_returns")},
        "variante": {k: variant[k] for k in ("return", "max_drawdown", "trades", "expectancy", "window_returns")},
        "achats_en_tendance_baissiere": extra, "ventes_variante": closed_variant,
        "ecart_rendement_points": round(observed * 100, 2),
        "intervalle_95_points": [round(low * 100, 2), round(high * 100, 2)],
    }
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
