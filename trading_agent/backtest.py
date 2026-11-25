"""Backtest : rejoue la stratégie sur des bougies historiques.

Règles pour rester honnête :
- à chaque bougie, la stratégie ne voit que le passé (aucune donnée future) ;
- un signal détecté à la clôture d'une bougie est exécuté à l'OUVERTURE de la
  bougie suivante, pas au prix du signal ;
- les mêmes moteur de risque et broker papier que le reste du système sont
  utilisés, avec les frais de la plateforme et un glissement fixe (les carnets
  d'ordres historiques ne sont pas disponibles).

Un bon résultat de backtest n'est pas une preuve de rentabilité future.
"""
import math
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

from trading_agent.config import Settings
from trading_agent.exchanges import PublicExchange
from trading_agent.hmm_regime import hmm_blocks, hmm_series
from trading_agent.paper import PaperBroker
from trading_agent.risk import RiskManager
from trading_agent.runner import closed_candles
from trading_agent.adversarial import objections as adversarial_objections
from trading_agent.regimes import can_enter, regime_series
from trading_agent.strategies import STRATEGIES, get_strategy, Strategy

YEAR_SECONDS = 365 * 86_400


async def fetch_history(ex, symbol: str, timeframe: str, days: int) -> list:
    """Télécharge les bougies closes des `days` derniers jours, par pages."""
    tf_ms = ex.timeframe_seconds(timeframe) * 1000
    now = ex.now_ms()
    since = now - days * 86_400_000
    by_ts = {}
    while since < now:
        batch = [c for c in await ex.ohlcv(symbol, timeframe=timeframe, limit=1000, since=since)
                 if c[0] >= since]
        if not batch:
            break
        for candle in batch:
            by_ts[candle[0]] = candle
        next_since = batch[-1][0] + tf_ms
        if next_since <= since:
            break
        since = next_since
    return closed_candles(sorted(by_ts.values()), tf_ms, now)


def max_drawdown(curve: list[float]) -> float:
    """Plus forte baisse depuis un sommet, en fraction (0.12 = -12 %)."""
    peak, worst = -math.inf, 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def sharpe(curve: list[float], tf_seconds: int) -> float | None:
    returns = [b / a - 1 for a, b in zip(curve, curve[1:]) if a > 0]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    std = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    return mean / std * math.sqrt(YEAR_SECONDS / tf_seconds) if std > 0 else None


_HMM_CACHE: dict[tuple, list] = {}


def cached_hmm_series(candles: list, refit_every: int = 7) -> list:
    """hmm_series ne dépend que des bougies : calculé une fois par série (plusieurs
    stratégies sont évaluées sur les mêmes bougies)."""
    if not candles:
        return []
    key = (len(candles), candles[0][0], candles[-1][0], float(candles[-1][4]), refit_every)
    if key not in _HMM_CACHE:
        if len(_HMM_CACHE) > 64:
            _HMM_CACHE.pop(next(iter(_HMM_CACHE)))
        _HMM_CACHE[key] = hmm_series(candles, refit_every=refit_every)
    return _HMM_CACHE[key]


def run_backtest(candles: list, settings: Settings, symbol: str, tf_seconds: int,
                 limits: dict | None = None, window: int = 300,
                 strategy: str | None = None, regime_filter: bool | None = None,
                 hmm_filter: bool | None = None, hmm_refit_every: int = 7) -> dict:
    # `strategy` : un nom, ou une stratégie déjà réglée (Learning Manager).
    # `hmm_filter` : même veto de Markov que le direct (réglage HMM_FILTER par défaut),
    # appliqué comme lui seulement quand le filtre de régime est actif.
    strat = strategy if isinstance(strategy, Strategy) else get_strategy(strategy or settings.strategy)
    use_filter = settings.regime_filter if regime_filter is None else regime_filter
    regimes = regime_series(candles)
    use_hmm = settings.hmm_filter if hmm_filter is None else hmm_filter
    hmms = cached_hmm_series(candles, hmm_refit_every) if use_hmm and use_filter else None
    entry_regime = pending_regime = None
    tf_ms = tf_seconds * 1000
    broker = PaperBroker(settings.paper_starting_cash, settings.paper_fee_rate,
                         settings.paper_slippage_bps, None)
    risk = RiskManager(settings)
    market = {"data_age_s": 0, "max_age_s": tf_seconds, "spread_bps": 0.0, **(limits or {})}
    pending = None
    exit_signal_ts = None   # sortie sur signal décidée à une clôture, exécutée à l'ouverture suivante
    pending_memo = position_ctx = None   # pour les stratégies dont la sortie connaît la position
    fills, curve, refusals = [], [], Counter()
    signals = cancelled = 0

    for i, candle in enumerate(candles):
        ts, open_, _high, _low, close, *_ = candle
        now = datetime.fromtimestamp((ts + tf_ms) / 1000, tz=timezone.utc)
        if pending:
            # Ordre décidé à la bougie précédente : exécuté à l'ouverture de celle-ci.
            if open_ > pending.stop_price:
                result = broker.buy(replace(pending, reference_price=float(open_)), candle_ms=ts - 1)
                if result["status"] == "filled":
                    fills.append({**result, "ts_ms": ts, "regime": pending_regime})
                    entry_regime = pending_regime
                    position_ctx = {"symbol": symbol, "entry_ts": ts, "memo": pending_memo}
                else:
                    refusals[result["reason"]] += 1
            else:
                cancelled += 1
            pending = None
        if exit_signal_ts is not None and symbol in broker.portfolio.positions:
            # Décidée sur la clôture précédente : on ne peut pas vendre au prix qui a servi
            # à décider. Simulée à l'ouverture de cette bougie (approximation, pas garantie).
            result = broker.close_position(symbol, float(open_), reason="exit_signal")
            fills.append({**result, "ts_ms": ts, "signal_ts_ms": exit_signal_ts, "entry_regime": entry_regime})
        exit_signal_ts = None
        broker.mark({symbol: float(close)}, now=now)
        history = candles[max(0, i - window + 1): i + 1]
        if symbol in broker.portfolio.positions:
            result = broker.check_stops(symbol, [candle])   # les stops restent dans la bougie
            wants_exit = (strat.exit_with(history, position_ctx) if strat.exit_with
                          else strat.exit(history))
            if not result and wants_exit:
                exit_signal_ts = ts
            if result:
                fills.append({**result, "ts_ms": ts, "entry_regime": entry_regime})
                broker.mark({}, now=now)
        else:
            signal = strat.entry(symbol, history)
            allowed, why = can_enter(strat, regimes[i]) if signal and use_filter else (True, "")
            if signal and allowed and hmms is not None:
                vetoed = hmm_blocks(strat, hmms[i])
                allowed, why = (False, vetoed) if vetoed else (True, why)
            majors = []
            if signal and allowed and settings.adversarial_in_backtest:
                majors = [o for o in adversarial_objections(
                    signal, history, regimes[i], tf_ms, settings.paper_fee_rate,
                    settings.paper_slippage_bps / 10_000, settings.reward_risk) if o["severity"] == "majeure"]
            if signal and not allowed:
                signals += 1
                refusals["régime : " + why] += 1
            elif signal and majors:
                signals += 1
                refusals["contradicteur : " + majors[0]["code"]] += 1
            elif signal:
                signals += 1
                decision = risk.evaluate(signal, broker.portfolio, market)
                if decision.approved:
                    pending, pending_regime = decision.order, regimes[i]
                    pending_memo = signal.memo
                else:
                    refusals[decision.reason] += 1
        curve.append(broker.portfolio.equity)

    sells = [f for f in fills if f["side"] == "sell"]
    wins = [f["realized_pnl"] for f in sells if f["realized_pnl"] > 0]
    losses = [f["realized_pnl"] for f in sells if f["realized_pnl"] <= 0]
    start = settings.paper_starting_cash
    return {
        "symbol": symbol,
        "strategy": strat.name,
        "strategy_label": strat.label,
        "candles": len(candles),
        "from": datetime.fromtimestamp(candles[0][0] / 1000, tz=timezone.utc) if candles else None,
        "to": datetime.fromtimestamp(candles[-1][0] / 1000, tz=timezone.utc) if candles else None,
        "start_equity": start,
        "end_equity": curve[-1] if curve else start,
        "return": (curve[-1] / start - 1) if curve else 0.0,
        "buy_and_hold_return": (candles[-1][4] / candles[0][4] - 1) if candles else 0.0,
        "max_drawdown": max_drawdown(curve),
        "sharpe": sharpe(curve, tf_seconds),
        "signals": signals,
        "trades": len(sells),
        "win_rate": len(wins) / len(sells) if sells else None,
        "profit_factor": sum(wins) / -sum(losses) if losses and sum(losses) < 0 else None,
        "average_trade": sum(f["realized_pnl"] for f in sells) / len(sells) if sells else None,
        "fees": sum(f["fee"] for f in fills),
        "stopped_out": sum(1 for f in sells if f["reason"] == "stop"),
        "cancelled_orders": cancelled,
        "refusals": dict(refusals),
        "open_position": symbol in broker.portfolio.positions,
        "equity_curve": curve,
        "fills": fills,
    }


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2%}"


def format_report(r: dict, exchange_id: str, timeframe: str, fee_rate: float) -> str:
    def num(value, fmt="{:.2f}"):
        return "n/a" if value is None else fmt.format(value)
    lines = [
        f"BACKTEST {r['symbol']} sur {exchange_id}, bougies {timeframe}, stratégie {r['strategy_label']}",
        f"Période          : {r['from']:%d/%m/%Y} → {r['to']:%d/%m/%Y} ({r['candles']} bougies)",
        f"Capital          : {r['start_equity']:.2f} € → {r['end_equity']:.2f} €",
        f"Rendement        : {pct(r['return'])}   (acheter et garder : {pct(r['buy_and_hold_return'])})",
        f"Perte max        : -{r['max_drawdown']:.2%} depuis un sommet",
        f"Sharpe annualisé : {num(r['sharpe'])}",
        f"Signaux / trades : {r['signals']} signaux, {r['trades']} trades clôturés, "
        f"dont {r['stopped_out']} sortis au stop",
        f"Taux de réussite : {num(r['win_rate'], '{:.0%}')}   Profit factor : {num(r['profit_factor'])}",
        f"Résultat moyen   : {num(r['average_trade'])} € par trade",
        f"Frais payés      : {r['fees']:.2f} € (taux {fee_rate:.2%} par ordre)",
    ]
    if r["refusals"]:
        lines.append("Refus du risque  : " + ", ".join(f"{k} ×{v}" for k, v in r["refusals"].items()))
    if r["cancelled_orders"]:
        lines.append(f"Ordres annulés   : {r['cancelled_orders']} (ouverture sous le stop)")
    if r["open_position"]:
        lines.append("Position encore ouverte à la fin (valorisée au dernier prix).")
    lines.append("Attention : un backtest ne prouve pas la rentabilité future.")
    return "\n".join(lines)


def compare_strategies(candles: list, settings: Settings, symbol: str, tf_seconds: int,
                       limits: dict | None = None) -> list[dict]:
    """Backtest de chaque stratégie sur la période entière et sur chaque moitié."""
    half = len(candles) // 2
    rows = []
    for name, strat in STRATEGIES.items():
        if strat.universe is not None:   # a besoin d'un panier de paires, pas d'une seule
            continue
        r = run_backtest(candles, settings, symbol, tf_seconds, limits, strategy=name)
        a = run_backtest(candles[:half], settings, symbol, tf_seconds, limits, strategy=name)
        b = run_backtest(candles[half:], settings, symbol, tf_seconds, limits, strategy=name)
        rows.append({**r, "first_half": a["return"], "second_half": b["return"]})
    return rows


def format_comparison(candles: list, settings: Settings, symbol: str, tf_seconds: int,
                      limits: dict, exchange_id: str, timeframe: str) -> str:
    """Compare toutes les stratégies sur la même période, puis sur chaque moitié
    séparément : une stratégie qui ne gagne que sur une moitié n'est pas fiable."""
    rows = compare_strategies(candles, settings, symbol, tf_seconds, limits)
    hold = candles[-1][4] / candles[0][4] - 1
    first = datetime.fromtimestamp(candles[0][0] / 1000, tz=timezone.utc)
    last = datetime.fromtimestamp(candles[-1][0] / 1000, tz=timezone.utc)
    lines = [
        f"COMPARAISON {symbol} sur {exchange_id}, bougies {timeframe}, "
        f"{first:%d/%m/%Y} → {last:%d/%m/%Y} ({len(candles)} bougies), frais {settings.paper_fee_rate:.2%}",
        f"Acheter et garder sur la période : {pct(hold)}",
        "",
        f"{'Stratégie':<22}{'Rendement':>10}{'1re moitié':>12}{'2e moitié':>11}"
        f"{'Perte max':>11}{'Trades':>8}{'Réussite':>10}{'P. factor':>11}{'Frais':>9}",
    ]
    for r in rows:
        win = "n/a" if r["win_rate"] is None else f"{r['win_rate']:.0%}"
        pf = "n/a" if r["profit_factor"] is None else f"{r['profit_factor']:.2f}"
        lines.append(
            f"{r['strategy_label']:<22}{pct(r['return']):>10}{pct(r['first_half']):>12}"
            f"{pct(r['second_half']):>11}{-r['max_drawdown']:>11.2%}{r['trades']:>8}{win:>10}"
            f"{pf:>11}{r['fees']:>8.0f}€")
    lines += ["", "Les 300 premières bougies de chaque période servent d'élan aux indicateurs.",
              "Attention : un backtest ne prouve pas la rentabilité future."]
    return "\n".join(lines)


async def run_backtest_cli(exchange_id: str, symbol: str, timeframe: str, days: int,
                           strategy: str | None = None):
    settings = Settings()
    if strategy and strategy != "all":
        settings.strategy = strategy
    settings.validate_safety()
    async with PublicExchange(exchange_id) as ex:
        market = (await ex.markets()).get(symbol)
        if not market or not market.get("spot"):
            raise ValueError(f"{symbol} is not a Spot market on {exchange_id}")
        taker = ex.fees(symbol).get("taker")
        if taker:
            settings.paper_fee_rate = taker
        tf_seconds = ex.timeframe_seconds(timeframe)
        candles = await fetch_history(ex, symbol, timeframe, days)
        limits = ex.limits(symbol)
    if len(candles) < 30:
        print(f"Pas assez de données : {len(candles)} bougies reçues.")
        return
    covered = (candles[-1][0] - candles[0][0]) / 86_400_000
    if covered < days * 0.9:
        print(f"Note : {exchange_id} n'a fourni que {covered:.0f} jours sur les {days} demandés.")
    if strategy == "all":
        print(format_comparison(candles, settings, symbol, tf_seconds, limits, exchange_id, timeframe))
        return
    report = run_backtest(candles, settings, symbol, tf_seconds, limits)
    print(format_report(report, exchange_id, timeframe, settings.paper_fee_rate))
