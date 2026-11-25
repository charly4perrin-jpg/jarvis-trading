import asyncio
from typing import Any
import ccxt.async_support as ccxt

from trading_agent.resilience import ExchangeHealth

SUPPORTED = ("bitvavo", "kraken")

class PublicExchange:
    """CCXT adapter restricted to unauthenticated public market endpoints."""
    def __init__(self, exchange_id: str):
        if exchange_id not in SUPPORTED:
            raise ValueError(f"Unsupported exchange: {exchange_id}")
        exchange_class = getattr(ccxt, exchange_id)
        self.client = exchange_class({
            "enableRateLimit": True,
            "timeout": 15000,
            "options": {"defaultType": "spot"},
        })
        self.id = exchange_id
        self.health = ExchangeHealth(exchange_id)

    async def __aenter__(self):
        await self.health.call("markets", self.client.load_markets)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.client.close()

    async def markets(self) -> dict[str, Any]:
        return await self.health.call("markets", self.client.load_markets)

    async def tickers(self, symbols: list[str] | None = None) -> dict[str, Any]:
        return await self.health.call("tickers", lambda: self.client.fetch_tickers(symbols))

    async def ohlcv(self, symbol: str, timeframe: str = "5m", limit: int = 100,
                    since: int | None = None):
        return await self.health.call("ohlcv", lambda: self.client.fetch_ohlcv(
            symbol, timeframe=timeframe, since=since, limit=limit))

    def fees(self, symbol: str) -> dict[str, Any]:
        """Frais publiés par la plateforme pour ce marché (None si inconnus)."""
        market = self.client.market(symbol) or {}
        return {"taker": market.get("taker"), "maker": market.get("maker")}

    async def order_book(self, symbol: str, limit: int = 20):
        return await self.health.call("order_book", lambda: self.client.fetch_order_book(symbol, limit=limit))

    async def ticker(self, symbol: str) -> dict[str, Any]:
        return await self.health.call("ticker", lambda: self.client.fetch_ticker(symbol))

    def limits(self, symbol: str) -> dict[str, Any]:
        """Taille et montant minimaux d'un ordre sur cette plateforme (None si inconnus)."""
        limits = (self.client.market(symbol) or {}).get("limits") or {}
        return {"min_amount": (limits.get("amount") or {}).get("min"),
                "min_cost": (limits.get("cost") or {}).get("min")}

    def timeframe_seconds(self, timeframe: str) -> int:
        return self.client.parse_timeframe(timeframe)

    def now_ms(self) -> int:
        return self.client.milliseconds()

    async def close(self):
        await self.client.close()

async def compare_exchanges(quote="EUR", min_quote_volume=50_000, limit=30):
    results = {}
    for exchange_id in SUPPORTED:
        try:
            async with PublicExchange(exchange_id) as ex:
                markets = await ex.markets()
                eligible_symbols = [
                    symbol for symbol, m in markets.items()
                    if m.get("spot") and m.get("active") and m.get("quote") == quote
                ]
                tickers = await ex.tickers(eligible_symbols)
                rows = []
                for symbol in eligible_symbols:
                    t = tickers.get(symbol) or {}
                    bid, ask = t.get("bid"), t.get("ask")
                    quote_volume = t.get("quoteVolume")
                    spread_bps = None
                    if bid and ask and bid > 0 and ask >= bid:
                        mid = (bid + ask) / 2
                        spread_bps = ((ask - bid) / mid) * 10_000
                    rows.append({
                        "symbol": symbol,
                        "last": t.get("last"),
                        "quote_volume_24h": quote_volume,
                        "spread_bps": spread_bps,
                        "base": (markets[symbol] or {}).get("base"),
                        "quote": quote,
                        "active": (markets[symbol] or {}).get("active"),
                        "volume_known": quote_volume is not None,
                    })
                rows.sort(
                    key=lambda r: (
                        r["quote_volume_24h"] is not None,
                        r["quote_volume_24h"] or -1
                    ),
                    reverse=True
                )
                results[exchange_id] = {
                    "markets_seen": len(eligible_symbols),
                    "markets_with_tickers": sum(1 for s in eligible_symbols if tickers.get(s)),
                    "liquid_candidates": [
                        r for r in rows
                        if r["quote_volume_24h"] is not None
                        and r["quote_volume_24h"] >= min_quote_volume
                        and (r["spread_bps"] is None or r["spread_bps"] <= 100)
                    ][:limit],
                    "top_markets": rows[:limit],
                }
        except Exception as exc:
            results[exchange_id] = {"error": f"{type(exc).__name__}: {exc}"}
    return results
