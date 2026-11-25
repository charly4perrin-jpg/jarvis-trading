"""Backtest de portefeuille : rejoue l'essai réel tel qu'il fonctionne vraiment.

Différence avec `backtest.run_backtest` (une paire, un capital) : ici UN capital est
partagé entre toutes les paires, avec les mêmes contrôles qu'en direct :
- nombre maximal de positions, exposition maximale, réserve de cash ;
- risque cumulé des positions corrélées (comme le Portfolio Optimizer) ;
- vitesse de perte, limites jour et semaine, pertes d'affilée ;
- filtre de régime et Contradicteur.

Règles anti-triche identiques : décision sur bougie close, exécution à l'ouverture de
la bougie suivante, aucune donnée future. Les paires sont parcourues à chaque date dans
un ordre fixe (alphabétique) : en cas d'opportunités simultanées, la première servie
consomme le capital, comme en direct où le scanner fixe l'ordre.
"""
import math
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

from trading_agent.adversarial import objections as adversarial_objections
from trading_agent.backtest import max_drawdown, sharpe
from trading_agent.config import Settings
from trading_agent.paper import PaperBroker
from trading_agent.regimes import can_enter, regime_series
from trading_agent.risk import RiskManager
from trading_agent.strategies import get_strategy


def run_portfolio(histories: dict[str, list], settings: Settings, tf_seconds: int,
                  strategies: list[str], window: int = 300, windows: int = 6) -> dict:
    from trading_agent.agents import correlation
    # Une stratégie qui classe les paires entre elles est liée au panier rejoué.
    strats = [(st.universe(histories) if st.universe is not None else st)
              for st in (get_strategy(s) for s in strategies)]
    by_name = {s.name: s for s in strats}
    tf_ms = tf_seconds * 1000
    symbols = sorted(histories)
    index = {s: {c[0]: i for i, c in enumerate(histories[s])} for s in symbols}
    regimes = {s: regime_series(histories[s]) for s in symbols}
    from trading_agent.backtest import cached_hmm_series
    from trading_agent.hmm_regime import hmm_blocks
    hmms = ({s: cached_hmm_series(histories[s]) for s in symbols}
            if settings.regime_filter and settings.hmm_filter else None)
    timeline = sorted({c[0] for s in symbols for c in histories[s]})
    broker = PaperBroker(settings.paper_starting_cash, settings.paper_fee_rate, settings.paper_slippage_bps, None)
    risk = RiskManager(settings)
    slip = settings.paper_slippage_bps / 10_000
    pending: dict[str, object] = {}
    exit_signals: dict[str, int] = {}   # symbole -> horodatage de la bougie du signal de sortie
    memos: dict[str, dict | None] = {}    # niveaux notés au signal, en attente d'exécution
    contexts: dict[str, dict] = {}        # position ouverte : date du fill et niveaux notés
    fills, curve, stamps, refusals = [], [], [], Counter()
    exposure_samples = []
    peak_positions = 0

    def closes_until(symbol: str, i: int) -> list:
        lo = max(0, i - settings.correlation_window - 1)
        return [(c[0], float(c[4])) for c in histories[symbol][lo:i + 1]]

    for ts in timeline:
        now = datetime.fromtimestamp((ts + tf_ms) / 1000, tz=timezone.utc)
        for symbol in symbols:
            i = index[symbol].get(ts)
            if i is None:
                continue
            candle = histories[symbol][i]
            _, open_, _h, _l, close, *_ = candle
            if symbol in exit_signals and symbol in broker.portfolio.positions:
                # Sortie décidée à la clôture précédente : exécutée à l'ouverture de cette bougie.
                result = broker.close_position(symbol, float(open_), reason="exit_signal")
                fills.append({**result, "ts_ms": ts, "signal_ts_ms": exit_signals[symbol]})
            exit_signals.pop(symbol, None)
            order = pending.pop(symbol, None)
            broker.portfolio.pending.pop(symbol, None)   # l'ordre quitte la file avant d'être exécuté
            if order is not None:
                if open_ > order.stop_price:
                    result = broker.buy(replace(order, reference_price=float(open_)), candle_ms=ts - 1)
                    if result["status"] == "filled":
                        fills.append({**result, "ts_ms": ts})
                        contexts[symbol] = {"symbol": symbol, "entry_ts": ts, "memo": memos.get(symbol)}
                    else:
                        refusals[result["reason"]] += 1
                else:
                    refusals["ordre annulé : ouverture sous le stop"] += 1
            broker.mark({symbol: float(close)}, now=now)
            history = histories[symbol][max(0, i - window + 1): i + 1]
            if symbol in broker.portfolio.positions:
                held = by_name.get(broker.portfolio.positions[symbol].get("strategy"), strats[0])
                result = broker.check_stops(symbol, [candle])
                wants_exit = (held.exit_with(history, contexts.get(symbol)) if held.exit_with
                              else held.exit(history))
                if not result and wants_exit:
                    exit_signals[symbol] = ts   # vendue à l'ouverture suivante, pas à cette clôture
                if result:
                    fills.append({**result, "ts_ms": ts})
                    broker.mark({}, now=now)
                continue
            if symbol in pending:
                continue
            for strat in strats:
                signal = strat.entry(symbol, history)
                if not signal:
                    continue
                if settings.regime_filter:
                    ok, why = can_enter(strat, regimes[symbol][i])
                    if ok and hmms is not None and hmm_blocks(strat, hmms[symbol][i]):
                        ok = False   # même veto de Markov que l'essai en direct
                    if not ok:
                        refusals["régime"] += 1
                        continue
                if settings.adversarial_in_backtest and any(
                        o["severity"] == "majeure" for o in adversarial_objections(
                            signal, history, regimes[symbol][i], tf_ms, settings.paper_fee_rate, slip,
                            settings.reward_risk)):
                    refusals["contradicteur"] += 1
                    continue
                # Portfolio Optimizer : risque déjà ouvert sur des positions corrélées.
                mine = closes_until(symbol, i)
                linked = 0.0
                exposures = [(o, p["quantity"] * max(0.0, p["mark_price"] - p["stop_price"]))
                             for o, p in broker.portfolio.positions.items()]
                exposures += [(o, q.quantity * max(0.0, q.reference_price - q.stop_price))
                              for o, q in pending.items()]          # ordres pas encore exécutés
                for other, open_risk in exposures:
                    j = index[other].get(ts)
                    rho = correlation(mine, closes_until(other, j if j is not None else len(histories[other]) - 1),
                                      settings.correlation_window)
                    if rho is None or rho >= settings.correlation_threshold:
                        linked += open_risk
                market = {"data_age_s": 0, "max_age_s": tf_seconds, "spread_bps": 0.0,
                          "correlated_open_risk": linked}
                decision = risk.evaluate(signal, broker.portfolio, market)
                if decision.approved:
                    o = decision.order
                    pending[symbol] = o
                    memos[symbol] = signal.memo
                    # Comme en direct : un ordre en attente compte dans les positions, l'exposition
                    # et le cash réservé, pour toutes les décisions suivantes.
                    broker.portfolio.pending[symbol] = {
                        "symbol": symbol, "quantity": o.quantity, "limit_price": o.reference_price,
                        "stop_price": o.stop_price, "reserved": o.quantity * o.reference_price * (1 + settings.paper_fee_rate)}
                    break
                refusals[decision.reason] += 1
        p = broker.portfolio
        peak_positions = max(peak_positions, len(p.positions) + len(pending))
        curve.append(p.equity)
        stamps.append(ts)
        exposure_samples.append(sum(x["quantity"] * x["mark_price"] for x in p.positions.values()) / p.equity
                                if p.equity else 0.0)

    sells = [f for f in fills if f["side"] == "sell"]
    pnls = [f["realized_pnl"] for f in sells]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    start = settings.paper_starting_cash
    n = len(curve)
    bounds = [round(k * n / windows) for k in range(windows + 1)]
    window_returns = [(curve[b - 1] / (curve[a - 1] if a else start) - 1) if b > a else 0.0
                      for a, b in zip(bounds, bounds[1:])]
    hold = []
    for s in symbols:
        c = histories[s]
        hold.append(float(c[-1][4]) / float(c[0][4]) - 1)
    years = (stamps[-1] - stamps[0]) / (365 * 86_400_000) if len(stamps) > 1 else 0
    total = curve[-1] / start - 1 if curve else 0.0
    return {
        "strategies": strategies, "symbols": symbols, "candles": n,
        "from": datetime.fromtimestamp(stamps[0] / 1000, tz=timezone.utc).date().isoformat() if stamps else None,
        "to": datetime.fromtimestamp(stamps[-1] / 1000, tz=timezone.utc).date().isoformat() if stamps else None,
        "return": total,
        "annual_return": (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else None,
        "buy_and_hold": sum(hold) / len(hold) if hold else None,
        "max_drawdown": max_drawdown(curve), "sharpe": sharpe(curve, tf_seconds),
        "trades": len(sells), "expectancy": sum(pnls) / len(pnls) if pnls else None,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
        "win_rate": len(wins) / len(pnls) if pnls else None,
        "fees": sum(f["fee"] for f in fills), "window_returns": window_returns,
        "average_exposure": sum(exposure_samples) / len(exposure_samples) if exposure_samples else 0.0,
        "peak_positions": peak_positions,
        "refusals": dict(refusals.most_common(8)), "equity_curve": curve, "fills": fills,
        "math_ok": all(math.isfinite(x) for x in curve),
    }


async def main(args) -> None:
    import asyncio
    from trading_agent.evaluate_cli import load_histories
    from trading_agent.exchanges import PublicExchange
    settings = Settings()
    strategies = [x.strip() for x in args.strategies.split(",") if x.strip()]
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    async with PublicExchange("bitvavo") as ex:
        try:
            histories = await load_histories(ex, symbols, args.timeframe, args.days)
        except ValueError as exc:
            raise SystemExit(f"{exc} Allongez la période (--days) : il faut au moins 400 bougies.")
        tf = ex.timeframe_seconds(args.timeframe)
    r = await asyncio.to_thread(run_portfolio, {k: v["candles"] for k, v in histories.items()}, settings, tf, strategies)

    def pct(x):
        return "n/a" if x is None else f"{x:+.1%}"
    print(f"PORTEFEUILLE {', '.join(strategies)} sur {len(symbols)} marchés, bougies {args.timeframe}, "
          f"du {r['from']} au {r['to']}, capital fictif {settings.paper_starting_cash:.0f} €")
    print(f"  Rendement {pct(r['return'])} (par an {pct(r['annual_return'])}), acheter-et-garder {pct(r['buy_and_hold'])}")
    print(f"  Perte maximale {r['max_drawdown']:.1%}, Sharpe {r['sharpe'] or 0:.2f}, exposition moyenne {r['average_exposure']:.0%}")
    print(f"  {r['trades']} trades, {r['expectancy'] or 0:+.2f} € par trade, ratio gains/pertes {r['profit_factor'] or 0:.2f}, frais {r['fees']:.0f} €")
    print("  Fenêtres : " + "  ".join(f"{x:+.1%}" for x in r["window_returns"]))
    print("  Un bon résultat sur le passé ne prouve pas la rentabilité future.")

