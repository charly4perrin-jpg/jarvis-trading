from datetime import datetime, timezone

from trading_agent.models import PaperOrder, Side
from trading_agent.paper import PaperBroker


def make_broker(tmp_path, cash=1000.0):
    return PaperBroker(cash, fee_rate=0.0, slippage_bps=0.0,
                       ledger_path=str(tmp_path / "ledger.jsonl"),
                       state_path=str(tmp_path / "state.json"))


def buy(broker, price=100.0, stop=95.0, qty=1.0, candle_ms=1000):
    return broker.buy(PaperOrder("BTC/EUR", Side.BUY, qty, price, stop), candle_ms=candle_ms)


def test_buy_then_close_realizes_pnl_with_fees(tmp_path):
    broker = PaperBroker(1000.0, fee_rate=0.01, slippage_bps=0.0,
                         ledger_path=str(tmp_path / "ledger.jsonl"))
    assert buy(broker)["status"] == "filled"
    result = broker.close_position("BTC/EUR", 110.0)
    # +10 de plus-value, -1 de frais d'achat, -1.1 de frais de vente
    assert round(result["realized_pnl"], 6) == 7.9
    assert round(broker.portfolio.cash, 6) == 1007.9
    assert broker.portfolio.positions == {}


def test_daily_and_weekly_pnl_follow_equity_and_reset(tmp_path):
    broker = make_broker(tmp_path)
    monday = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
    broker.mark({}, now=monday)
    buy(broker)
    broker.mark({"BTC/EUR": 80.0}, now=monday)
    assert broker.portfolio.daily_pnl == -20.0
    assert broker.portfolio.weekly_pnl == -20.0
    broker.mark({"BTC/EUR": 80.0}, now=datetime(2026, 11, 17, 10, tzinfo=timezone.utc))
    assert broker.portfolio.daily_pnl == 0.0      # nouveau jour
    assert broker.portfolio.weekly_pnl == -20.0   # même semaine
    broker.mark({"BTC/EUR": 80.0}, now=datetime(2026, 11, 23, 10, tzinfo=timezone.utc))
    assert broker.portfolio.weekly_pnl == 0.0     # nouvelle semaine


def test_stop_closes_position_at_stop_or_gap_open(tmp_path):
    broker = make_broker(tmp_path)
    buy(broker, candle_ms=1000)
    # Bougie déjà vérifiée (celle de l'entrée) : ignorée même si elle touche le stop.
    assert broker.check_stops("BTC/EUR", [[1000, 100, 101, 90, 100, 1]]) is None
    # Bougie suivante sans toucher le stop : position conservée.
    assert broker.check_stops("BTC/EUR", [[2000, 100, 101, 96, 100, 1]]) is None
    result = broker.check_stops("BTC/EUR", [[3000, 97, 98, 94, 96, 1]])
    assert result["reason"] == "stop" and result["fill_price"] == 95.0

    buy(broker, candle_ms=3000)
    gap = broker.check_stops("BTC/EUR", [[4000, 90, 91, 88, 89, 1]])
    assert gap["fill_price"] == 90.0  # ouverture sous le stop


def test_portfolio_survives_restart(tmp_path):
    broker = make_broker(tmp_path)
    buy(broker)
    restarted = make_broker(tmp_path, cash=5000.0)
    assert restarted.portfolio.cash == 900.0
    assert restarted.portfolio.positions["BTC/EUR"]["stop_price"] == 95.0


def test_consecutive_losses_count_and_reset(tmp_path):
    broker = make_broker(tmp_path)
    day = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)
    broker.mark({}, now=day)
    for _ in range(2):
        buy(broker)
        broker.close_position("BTC/EUR", 90.0)
    assert broker.portfolio.consecutive_losses == 2
    buy(broker)
    broker.close_position("BTC/EUR", 120.0)
    assert broker.portfolio.consecutive_losses == 0   # un gain remet à zéro
    buy(broker)
    broker.close_position("BTC/EUR", 90.0)
    broker.mark({}, now=datetime(2026, 11, 17, 10, tzinfo=timezone.utc))
    assert broker.portfolio.consecutive_losses == 0   # nouveau jour
