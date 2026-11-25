"""Commandes evaluate / status / promote / demote."""
from trading_agent.backtest import fetch_history
from trading_agent.config import Settings
from trading_agent.evaluate import STATUS_LABELS, Registry, evaluate_strategy, format_evaluation
from trading_agent.events import Event, EventBus
from trading_agent.exchanges import PublicExchange
from trading_agent.strategies import STRATEGIES


async def load_histories(ex, symbols: list[str], timeframe: str, days: int) -> dict:
    markets = await ex.markets()
    histories = {}
    for symbol in symbols:
        market = markets.get(symbol)
        if not market or not market.get("spot"):
            raise ValueError(f"{symbol} n'existe pas en Spot sur cette plateforme.")
        candles = await fetch_history(ex, symbol, timeframe, days)
        if len(candles) < 400:
            raise ValueError(f"Historique trop court pour {symbol} : {len(candles)} bougies.")
        histories[symbol] = {"candles": candles, "limits": ex.limits(symbol),
                             "fee": ex.fees(symbol).get("taker")}
    return histories


async def evaluate_all(ex, settings: Settings, registry: Registry, symbols: list[str],
                       timeframe: str, days: int) -> list[dict]:
    import asyncio
    histories = await load_histories(ex, symbols, timeframe, days)
    tf_seconds = ex.timeframe_seconds(timeframe)
    from trading_agent.evaluate import add_deflated_criterion
    from trading_agent.overfitting import returns_of, sharpe_per_period
    from trading_agent.portfolio_backtest import run_portfolio
    candles = {k: v["candles"] for k, v in histories.items()}
    reports, portfolio_returns = [], {}
    for name in STRATEGIES:
        report = await asyncio.to_thread(evaluate_strategy, histories, settings, tf_seconds,
                                         name, settings.accept_windows)
        report["timeframe"], report["days"] = timeframe, days
        run = await asyncio.to_thread(run_portfolio, candles, settings, tf_seconds, [name])
        portfolio_returns[name] = returns_of(run["equity_curve"])
        reports.append(report)
    sharpes = [sharpe_per_period(r) for r in portfolio_returns.values()]
    n_trials = max(settings.research_trials, len(STRATEGIES))
    for report in reports:
        add_deflated_criterion(report, portfolio_returns[report["strategy"]], sharpes, n_trials,
                               settings.accept_min_deflated_sharpe)
        registry.save_evaluation(report)
    return reports


def record(settings: Settings, text: str, payload: dict) -> None:
    """Toute modification du registre est écrite dans le journal chaîné."""
    bus = EventBus(settings.event_log_path, settings.halt_flag_path, settings.database_path)
    bus.publish(Event(source="coordinator", target="coordinator", type="registry.change",
                      correlation_id="manual", result=text, payload=payload))


async def main(args) -> None:
    settings = Settings()
    registry = Registry(settings.registry_path, settings.evaluations_dir)
    if args.command == "evaluate":
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        async with PublicExchange(args.exchange) as ex:
            reports = await evaluate_all(ex, settings, registry, symbols, args.timeframe, args.days)
        for report in reports:
            print(format_evaluation(report), end="\n\n")
        print("Un bon résultat sur le passé ne prouve pas la rentabilité future.")
    elif args.command == "status":
        for row in registry.overview():
            ev = row["evaluation"]
            note = "jamais évaluée" if not ev else (
                f"{sum(c['passed'] for c in ev['criteria'])} critères sur {len(ev['criteria'])} remplis")
            print(f"{row['label']:<22} {row['status_label']:<18} {note}")
    elif args.command == "promote":
        if args.confirm != args.strategy:
            print("Confirmation incorrecte : retapez exactement le nom de la stratégie.")
            return
        try:
            before = registry.status(args.strategy)
            evidence = registry.latest_evaluation(args.strategy) if before == "candidate" else None
            after = registry.promote(args.strategy, evidence, by="ligne de commande")
        except (PermissionError, ValueError) as exc:
            print(f"Promotion refusée. {exc}")
            return
        text = f"Stratégie {args.strategy} : {STATUS_LABELS[before]} → {STATUS_LABELS[after]}."
        record(settings, text, {"strategy": args.strategy, "from": before, "to": after})
        print(text)
    else:
        before = registry.status(args.strategy)
        registry.demote(args.strategy, "ligne de commande", args.reason)
        text = f"Stratégie {args.strategy} renvoyée au statut candidate ({args.reason})."
        record(settings, text, {"strategy": args.strategy, "from": before, "to": "candidate"})
        print(text)
