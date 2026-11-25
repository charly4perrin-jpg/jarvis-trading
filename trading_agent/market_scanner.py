
import asyncio
from typing import Any

import ccxt.async_support as ccxt


class MarketScanner:
    """Scanner public des marchés EUR Spot avec analyse de profondeur."""

    def __init__(
        self,
        min_quote_volume: float = 50_000,
        max_spread_bps: float = 10,
        order_book_markets: int = 20,
        depth_band_pct: float = 0.5,
    ):
        self.min_quote_volume = min_quote_volume
        self.max_spread_bps = max_spread_bps
        self.order_book_markets = order_book_markets
        self.depth_band_pct = depth_band_pct

    def calculate_depth(
        self,
        order_book: dict[str, Any],
        mid_price: float,
    ) -> tuple[float, float]:
        """Calcule la profondeur EUR dans une zone autour du prix médian."""

        band = self.depth_band_pct / 100

        bid_limit = mid_price * (1 - band)
        ask_limit = mid_price * (1 + band)
        tolerance = mid_price * 1e-12

        bid_depth = sum(
    level[0] * level[1]
    for level in order_book.get("bids", [])
    if level[0] >= bid_limit - tolerance
)
        

        ask_depth = sum(
    level[0] * level[1]
    for level in order_book.get("asks", [])
    if level[0] <= ask_limit + tolerance
)

        return bid_depth, ask_depth

    async def scan(self, ex, exchange_id: str) -> list[dict[str, Any]]:
        """Même analyse que `scan_exchange`, mais avec une connexion déjà ouverte
        (PublicExchange ou plateforme simulée) : utilisé par les agents."""
        markets = await ex.markets()
        tickers = await ex.tickers()
        results = []
        for symbol, market in markets.items():
            if market.get("quote") != "EUR" or market.get("active") is False or market.get("spot") is False:
                continue
            ticker = tickers.get(symbol) or {}
            last, bid, ask = ticker.get("last"), ticker.get("bid"), ticker.get("ask")
            if not last or not bid or not ask or last <= 0 or bid <= 0 or ask < bid:
                continue
            quote_volume = ticker.get("quoteVolume")
            if quote_volume is None:
                if ticker.get("baseVolume") is None:
                    continue
                quote_volume = ticker["baseVolume"] * last
            mid = (bid + ask) / 2
            spread_bps = (ask - bid) / mid * 10_000
            if quote_volume < self.min_quote_volume or spread_bps > self.max_spread_bps:
                continue
            results.append({"exchange": exchange_id, "symbol": symbol, "last": last,
                            "quote_volume_24h": quote_volume, "spread_bps": spread_bps,
                            "bid": bid, "ask": ask, "bid_depth_eur_0_5pct": None,
                            "ask_depth_eur_0_5pct": None, "depth_error": None})
        results.sort(key=lambda item: item["quote_volume_24h"], reverse=True)
        for market in results[:self.order_book_markets]:
            try:
                book = await ex.order_book(market["symbol"], limit=20)
                bid_depth, ask_depth = self.calculate_depth(book, (market["bid"] + market["ask"]) / 2)
                market["bid_depth_eur_0_5pct"], market["ask_depth_eur_0_5pct"] = bid_depth, ask_depth
            except Exception as exc:
                market["depth_error"] = str(exc)
        return results

    async def scan_exchange(self, exchange_id: str) -> list[dict[str, Any]]:
        exchange_class = getattr(ccxt, exchange_id, None)

        if exchange_class is None:
            raise ValueError(f"Plateforme inconnue : {exchange_id}")

        exchange = exchange_class({
            "enableRateLimit": True,
            "timeout": 20000,
        })

        try:
            markets = await exchange.load_markets()
            tickers = await exchange.fetch_tickers()

            results = []

            for symbol, market in markets.items():
                if (
                    market.get("quote") != "EUR"
                    or market.get("active") is False
                    or market.get("spot") is False
                ):
                    continue

                ticker = tickers.get(symbol)

                if not ticker:
                    continue

                last = ticker.get("last")
                bid = ticker.get("bid")
                ask = ticker.get("ask")

                if not last or not bid or not ask:
                    continue

                if last <= 0 or bid <= 0 or ask < bid:
                    continue

                quote_volume = ticker.get("quoteVolume")

                if quote_volume is None:
                    base_volume = ticker.get("baseVolume")
                    if base_volume is None:
                        continue
                    quote_volume = base_volume * last

                if quote_volume < self.min_quote_volume:
                    continue

                mid_price = (bid + ask) / 2
                spread_bps = ((ask - bid) / mid_price) * 10_000

                if spread_bps > self.max_spread_bps:
                    continue

                results.append({
                    "exchange": exchange_id,
                    "symbol": symbol,
                    "last": last,
                    "quote_volume_24h": quote_volume,
                    "spread_bps": spread_bps,
                    "bid": bid,
                    "ask": ask,
                    "bid_depth_eur_0_5pct": None,
                    "ask_depth_eur_0_5pct": None,
                    "depth_error": None,
                })

            results.sort(
                key=lambda item: item["quote_volume_24h"],
                reverse=True,
            )

            # On récupère les carnets uniquement pour les marchés
            # les plus liquides afin de limiter les requêtes API.
            for market in results[:self.order_book_markets]:
                try:
                    order_book = await exchange.fetch_order_book(
                        market["symbol"],
                        limit=20,
                    )

                    bid_depth, ask_depth = self.calculate_depth(
                        order_book,
                        (market["bid"] + market["ask"]) / 2,
                    )

                    market["bid_depth_eur_0_5pct"] = bid_depth
                    market["ask_depth_eur_0_5pct"] = ask_depth

                except Exception as error:
                    market["depth_error"] = str(error)

            return results

        finally:
            await exchange.close()


async def main():
    scanner = MarketScanner(
        min_quote_volume=50_000,
        max_spread_bps=10,
        order_book_markets=20,
        depth_band_pct=0.5,
    )

    for exchange_id in ("bitvavo", "kraken"):
        print(f"\n{'=' * 65}")
        print(f"SCAN : {exchange_id.upper()}")
        print("=" * 65)

        try:
            results = await scanner.scan_exchange(exchange_id)

            print(f"Marchés retenus : {len(results)}")
            print("Profondeur affichée pour les 20 premiers marchés.")
            print()

            for market in results[:20]:
                bid_depth = market["bid_depth_eur_0_5pct"]
                ask_depth = market["ask_depth_eur_0_5pct"]

                print(
                    f"{market['symbol']:15} "
                    f"Volume: {market['quote_volume_24h']:12,.0f} EUR | "
                    f"Spread: {market['spread_bps']:.2f} bps"
                )

                if market["depth_error"]:
                    print(f"  Erreur carnet : {market['depth_error']}")
                elif bid_depth is not None and ask_depth is not None:
                    print(
                        f"  Profondeur achat: {bid_depth:,.0f} EUR | "
                        f"vente: {ask_depth:,.0f} EUR"
                    )
                else:
                    print("  Profondeur indisponible")

        except Exception as error:
            print(f"Erreur {exchange_id}: {error}")


if __name__ == "__main__":
    asyncio.run(main())
