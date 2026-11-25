import asyncio
from ccxt.base.errors import NetworkError
from trading_agent.config import Settings
from trading_agent.exchanges import PublicExchange
from trading_agent.strategies import momentum_demo, momentum_exit
from trading_agent.risk import RiskManager
from trading_agent.paper import PaperBroker
from trading_agent.audit import AuditLog

def closed_candles(candles: list, tf_ms: int, now_ms: int) -> list:
    """Ne garde que les bougies terminées (la dernière est souvent encore en formation)."""
    return [c for c in candles if c[0] + tf_ms <= now_ms]

def seconds_to_next_candle(now_s: float, tf_seconds: int, delay: float = 20) -> float:
    """Temps d'attente jusqu'à la prochaine clôture de bougie (+ une marge pour que
    la plateforme publie la bougie)."""
    return (now_s // tf_seconds + 1) * tf_seconds + delay - now_s

async def run_paper(exchange_id, symbol, timeframe="5m", cycles=1):
    s = Settings()
    s.validate_safety()
    audit = AuditLog(s.audit_log_path)
    broker = PaperBroker(s.paper_starting_cash, s.paper_fee_rate,
                         s.paper_slippage_bps, s.paper_ledger_path, s.paper_state_path)
    risk = RiskManager(s)

    async with PublicExchange(exchange_id) as ex:
        market = (await ex.markets()).get(symbol)
        if not market or not market.get("spot") or not market.get("active"):
            raise ValueError(f"{symbol} is not an active Spot market on {exchange_id}")
        tf_seconds = ex.client.parse_timeframe(timeframe)
        for cycle in range(cycles):
            if cycle:
                await asyncio.sleep(tf_seconds)
            try:
                candles = await ex.ohlcv(symbol, timeframe=timeframe, limit=100)
            except NetworkError as exc:
                # Coupure réseau : on n'agit pas sur ce cycle (fail-closed) et on réessaie au suivant.
                audit.write("network_error", {"exchange": exchange_id, "error": str(exc)})
                print(f"{exchange_id} {symbol}: network error, cycle skipped.")
                continue
            candles = closed_candles(candles, tf_seconds * 1000, ex.client.milliseconds())
            if not candles:
                raise RuntimeError("No closed OHLCV candle returned.")
            last_price = float(candles[-1][4])
            broker.mark({symbol: last_price})
            audit.write("market_snapshot", {
                "exchange": exchange_id, "symbol": symbol,
                "timeframe": timeframe, "last_price": last_price,
                "candle_count": len(candles)
            })
            if symbol in broker.portfolio.positions:
                result = broker.check_stops(symbol, candles)
                if not result and momentum_exit(candles):
                    result = broker.close_position(symbol, last_price, reason="exit_signal")
                if result:
                    audit.write("paper_execution", result)
                    print(result)
                else:
                    print(f"{exchange_id} {symbol}: position held.")
                continue
            signal = momentum_demo(symbol, candles)
            if signal:
                decision = risk.evaluate(signal, broker.portfolio)
                audit.write("risk_decision", {
                    "symbol": symbol, "approved": decision.approved,
                    "reason": decision.reason, "signal": signal
                })
                if decision.approved:
                    result = broker.buy(decision.order, candle_ms=candles[-1][0])
                    audit.write("paper_execution", result)
                    print(result)
                else:
                    print({"status": "blocked", "reason": decision.reason})
            else:
                print(f"{exchange_id} {symbol}: no demo signal; no trade.")

async def run_compare(quote="EUR", min_quote_volume=50_000, limit=30):
    from trading_agent.exchanges import compare_exchanges
    results = await compare_exchanges(quote, min_quote_volume, limit)
    for exchange, data in results.items():
        print(f"\n=== {exchange.upper()} ===")
        if "error" in data:
            print(data["error"])
            continue
        print(f"Active {quote} Spot markets: {data['markets_seen']}")
        print(f"Markets with ticker data: {data['markets_with_tickers']}")
        for row in data["liquid_candidates"][:10]:
            print(row)
