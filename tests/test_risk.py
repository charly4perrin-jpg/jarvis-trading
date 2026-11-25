from datetime import datetime, timezone
from trading_agent.config import Settings
from trading_agent.models import PaperPortfolio, Signal, Side
from trading_agent.risk import RiskManager

def signal():
    return Signal("BTC/EUR", Side.BUY, .9, 50000, 49000, "test", datetime.now(timezone.utc))

def test_approves_within_limits():
    d = RiskManager(Settings()).evaluate(signal(), PaperPortfolio(3000, {}, 3000))
    assert d.approved and d.order.quantity > 0

def test_blocks_daily_limit():
    p = PaperPortfolio(3000, {}, 3000, daily_pnl=-61)
    d = RiskManager(Settings()).evaluate(signal(), p)
    assert not d.approved and "Daily loss" in d.reason

def test_blocks_short_or_sell():
    s = Signal("BTC/EUR", Side.SELL, .99, 50000, 51000, "test", datetime.now(timezone.utc))
    d = RiskManager(Settings()).evaluate(s, PaperPortfolio(3000, {}, 3000))
    assert not d.approved


GOOD_MARKET = {"data_age_s": 10, "max_age_s": 300, "spread_bps": 2.0,
               "min_amount": 0.0001, "min_cost": 5.0}


def evaluate(market=GOOD_MARKET, portfolio=None, **settings):
    s = Settings(_env_file=None, **settings)
    return s, RiskManager(s).evaluate(signal(), portfolio or PaperPortfolio(3000, {}, 3000), market)


def test_loss_at_stop_including_fees_and_slippage_stays_within_budget():
    s, d = evaluate()
    assert d.approved
    slip, fee = s.paper_slippage_bps / 10_000, s.paper_fee_rate
    entry, exit_ = 50000 * (1 + slip), 49000 * (1 - slip)
    loss = d.order.quantity * ((entry - exit_) + fee * (entry + exit_))
    assert loss <= 3000 * s.risk_per_trade_pct + 1e-6
    # Sans frais ni glissement, la position aurait été plus grosse.
    assert d.order.quantity < 3000 * s.risk_per_trade_pct / 1000


def test_blocks_stale_or_missing_data():
    assert "stale" in evaluate({**GOOD_MARKET, "data_age_s": 301})[1].reason
    assert "stale" in evaluate({**GOOD_MARKET, "data_age_s": None})[1].reason
    assert "stale" in evaluate({})[1].reason


def test_blocks_wide_or_unknown_spread():
    assert "Spread" in evaluate({**GOOD_MARKET, "spread_bps": 41})[1].reason
    assert "Spread" in evaluate({**GOOD_MARKET, "spread_bps": None})[1].reason


def test_blocks_after_consecutive_losses():
    p = PaperPortfolio(3000, {}, 3000, consecutive_losses=3)
    assert "Consecutive" in evaluate(portfolio=p)[1].reason


def test_blocks_order_below_exchange_minimum():
    assert "minimum" in evaluate({**GOOD_MARKET, "min_cost": 10_000})[1].reason
    assert "minimum" in evaluate({**GOOD_MARKET, "min_amount": 1.0})[1].reason
