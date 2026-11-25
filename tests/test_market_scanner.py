
from unittest.mock import AsyncMock, patch

import pytest

from trading_agent.market_scanner import MarketScanner


@pytest.mark.asyncio
async def test_scanner_keeps_valid_eur_market_and_checks_depth():
    fake_exchange = AsyncMock()

    fake_exchange.load_markets.return_value = {
        "BTC/EUR": {
            "quote": "EUR",
            "active": True,
            "spot": True,
        },
        "ETH/EUR": {
            "quote": "EUR",
            "active": True,
            "spot": True,
        },
    }

    fake_exchange.fetch_tickers.return_value = {
        "BTC/EUR": {
            "last": 50000,
            "bid": 49990,
            "ask": 50010,
            "quoteVolume": 100000,
        },
        "ETH/EUR": {
            "last": 2000,
            "bid": 1990,
            "ask": 2010,
            "quoteVolume": 100000,
        },
    }

    fake_exchange.fetch_order_book.return_value = {
        "bids": [
            [49990, 1],
            [49750, 2],
            [49749, 100],
        ],
        "asks": [
            [50010, 1],
            [50250, 2],
            [50251, 100],
        ],
    }

    with patch(
        "trading_agent.market_scanner.getattr",
        create=True,
        return_value=lambda config: fake_exchange,
    ):
        scanner = MarketScanner(
            min_quote_volume=50000,
            max_spread_bps=10,
            order_book_markets=20,
            depth_band_pct=0.5,
        )

        results = await scanner.scan_exchange("bitvavo")

    assert len(results) == 1
    assert results[0]["symbol"] == "BTC/EUR"

    assert results[0]["bid_depth_eur_0_5pct"] == 149490
    assert results[0]["ask_depth_eur_0_5pct"] == 150510

    fake_exchange.fetch_order_book.assert_awaited_once()
    fake_exchange.close.assert_awaited_once()
