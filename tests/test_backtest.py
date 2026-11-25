import math

from trading_agent.backtest import max_drawdown, run_backtest, sharpe
from trading_agent.config import Settings
from trading_agent.models import PaperOrder, Side
from trading_agent.paper import PaperBroker, walk_book


def series(n=400):
    """Prix ondulant avec une légère tendance : produit des entrées et des sorties."""
    closes = [100 + 15 * math.sin(i / 12) + i * 0.02 for i in range(n)]
    candles = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        candles.append([i * 3_600_000, o, max(o, c) + 0.3, min(o, c) - 0.3, c, 1.0])
    return candles


def settings(**kwargs):
    return Settings(_env_file=None, **{"regime_filter": False, **kwargs})


def test_backtest_trades_and_reports_consistent_numbers():
    r = run_backtest(series(), settings(), "BTC/EUR", 3600)
    assert r["trades"] > 0 and r["signals"] >= r["trades"]
    assert r["fees"] > 0
    assert len(r["equity_curve"]) == r["candles"] == 400
    assert math.isclose(r["end_equity"], r["equity_curve"][-1])
    assert 0 <= r["max_drawdown"] < 1
    assert 0 <= r["win_rate"] <= 1


def test_backtest_never_looks_into_the_future():
    candles = series()
    full = run_backtest(candles, settings(), "BTC/EUR", 3600)["equity_curve"]
    for cut in (150, 260, 333):
        partial = run_backtest(candles[:cut], settings(), "BTC/EUR", 3600)["equity_curve"]
        assert partial == full[:cut]


def test_higher_fees_give_worse_result():
    cheap = run_backtest(series(), settings(paper_fee_rate=0.0), "BTC/EUR", 3600)
    costly = run_backtest(series(), settings(paper_fee_rate=0.01), "BTC/EUR", 3600)
    assert costly["end_equity"] < cheap["end_equity"]


def test_entry_is_filled_at_next_candle_open():
    candles = [[i * 60_000, 100.0, 101.0, 99.0, 100.0, 1.0] for i in range(30)]
    candles.append([30 * 60_000, 100.0, 131.0, 99.0, 130.0, 1.0])   # signal à la clôture
    candles.append([31 * 60_000, 140.0, 141.0, 139.0, 140.0, 1.0])  # exécution à l'ouverture
    r = run_backtest(candles, settings(paper_fee_rate=0.0, paper_slippage_bps=0.0), "BTC/EUR", 60)
    assert r["open_position"] and r["signals"] == 1
    assert r["end_equity"] == r["start_equity"]  # acheté 140, valorisé 140 : ni gain ni perte


def test_max_drawdown_and_sharpe():
    assert math.isclose(max_drawdown([100, 120, 90, 130]), 0.25)
    assert max_drawdown([100, 110, 120]) == 0
    assert sharpe([100, 100, 100], 3600) is None
    assert sharpe([100, 101, 103, 104], 3600) > 0


def test_walk_book_average_price_and_limit():
    asks = [[100.0, 1.0], [101.0, 1.0], [110.0, 5.0]]
    assert walk_book(asks, 1.5, 105.0, buying=True) == (1.5, (100 + 0.5 * 101) / 1.5)
    assert walk_book(asks, 5.0, 105.0, buying=True)[0] == 2.0     # s'arrête à la limite de prix
    assert walk_book(asks, 1.0, 99.0, buying=True) == (0.0, 0.0)  # prix parti trop haut


def test_partial_buy_then_partial_sell_keep_books_consistent(tmp_path):
    broker = PaperBroker(10_000.0, 0.0, 0.0, None, max_slippage_bps=100)
    order = PaperOrder("BTC/EUR", Side.BUY, 5.0, 100.0, 95.0)
    bought = broker.buy(order, asks=[[100.0, 2.0], [100.5, 1.0], [150.0, 9.0]])
    assert bought["partial"] and bought["quantity"] == 3.0
    sold = broker.close_position("BTC/EUR", 100.0, bids=[[100.0, 1.0], [90.0, 9.0]])
    assert sold["partial"] and sold["quantity"] == 1.0
    assert broker.portfolio.positions["BTC/EUR"]["quantity"] == 2.0
    rejected = broker.buy(PaperOrder("ETH/EUR", Side.BUY, 1.0, 100.0, 95.0), asks=[[102.0, 5.0]])
    assert rejected["status"] == "rejected"


def test_simulated_clock_is_kept_between_marks():
    """En backtest, une vente ne doit pas faire revenir le portefeuille à la date réelle
    (sinon les compteurs de pertes du jour seraient remis à zéro à chaque trade)."""
    from datetime import datetime, timezone
    broker = PaperBroker(1000.0, 0.0, 0.0, None)
    broker.mark({}, now=datetime(2020, 1, 6, 10, tzinfo=timezone.utc))
    for _ in range(2):
        broker.buy(PaperOrder("BTC/EUR", Side.BUY, 1.0, 100.0, 95.0))
        broker.close_position("BTC/EUR", 90.0)
    assert broker.portfolio.day_key == "2020-01-06"
    assert broker.portfolio.consecutive_losses == 2
    assert broker.portfolio.daily_pnl == -20.0


def test_every_strategy_runs_without_looking_ahead():
    from trading_agent.strategies import STRATEGIES
    candles = series(700)
    for name in STRATEGIES:
        full = run_backtest(candles, settings(), "BTC/EUR", 3600, strategy=name)
        assert full["strategy"] == name
        partial = run_backtest(candles[:520], settings(), "BTC/EUR", 3600, strategy=name)
        assert partial["equity_curve"] == full["equity_curve"][:520]


def test_backtest_applies_the_same_contradictor_rules_as_live():
    from datetime import datetime, timezone
    from trading_agent.models import Side, Signal
    from trading_agent.strategies import Strategy

    def tight_entry(symbol, candles):
        if len(candles) < 30 or len(candles) % 20:
            return None
        price = float(candles[-1][4])
        return Signal(symbol, Side.BUY, 0.8, price, price * 0.999, "stop collé au prix",
                      datetime.now(timezone.utc), "serre")
    tight = Strategy("serre", "stop serré", "test", tight_entry, lambda c: False, "rien", "rien")
    candles = [[i * 3_600_000, 100.0 + i * 0.01, 100.5 + i * 0.01, 99.5 + i * 0.01, 100.0 + i * 0.01, 10.0]
               for i in range(300)]
    on = run_backtest(candles, settings(adversarial_in_backtest=True), "BTC/EUR", 3600, strategy=tight)
    off = run_backtest(candles, settings(adversarial_in_backtest=False), "BTC/EUR", 3600, strategy=tight)
    assert on["refusals"].get("contradicteur : frais", 0) > 0 and on["trades"] == 0
    assert off["signals"] == on["signals"] and "contradicteur : frais" not in off["refusals"]
