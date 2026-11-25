"""Sorties sur signal : décidées à une clôture, simulées à l'ouverture suivante (jamais au
prix qui a servi à décider). Les stops, eux, restent vérifiés dans la bougie."""
from datetime import datetime, timezone

import pytest

from trading_agent import strategies
from trading_agent.backtest import run_backtest
from trading_agent.config import Settings
from trading_agent.models import Side, Signal
from trading_agent.portfolio_backtest import run_portfolio
from trading_agent.strategies import Strategy

D = 86_400_000
ENTRY_AT, EXIT_AT = 3, 6          # longueur d'historique où le signal apparaît


def settings():
    return Settings(_env_file=None, regime_filter=False, adversarial_in_backtest=False,
                    hmm_filter=False, paper_slippage_bps=10)


def scripted(exit_at=EXIT_AT):
    def entry(symbol, history):
        if len(history) == ENTRY_AT:
            close = float(history[-1][4])
            return Signal(symbol, Side.BUY, 0.8, close, close * 0.8, "test", datetime.now(timezone.utc))
        return None

    def exit_(history):
        return len(history) == exit_at

    return Strategy("scripted", "scénario", "test", entry, exit_, "rien", "sortie de test")


def candles(n=9, gap_open=90.0, low_at=None):
    out = []
    for i in range(n):
        o = c = 100.0
        lo = 99.0
        if i == EXIT_AT:            # première cotation après le signal (clôture 100 à l'indice EXIT_AT-1)
            o, lo = gap_open, gap_open - 1
        if low_at is not None and i == low_at:
            lo = 70.0
        out.append([i * D, o, max(o, c) + 1, lo, c, 1000.0])
    return out


def sells(fills):
    return [f for f in fills if f["side"] == "sell"]


def test_signal_exit_is_sold_at_next_open_never_at_the_deciding_close():
    r = run_backtest(candles(), settings(), "BTC/EUR", 86_400, window=300, strategy=scripted())
    out = sells(r["fills"])
    assert len(out) == 1
    sale = out[0]
    assert sale["reason"] == "exit_signal"
    assert sale["signal_ts_ms"] == (EXIT_AT - 1) * D        # signal daté de la clôture à 100 €
    assert sale["ts_ms"] == EXIT_AT * D                      # exécution à l'ouverture suivante
    assert sale["fill_price"] == pytest.approx(90.0 * (1 - 0.001))
    assert sale["fill_price"] < 95                           # aucune vente simulée à 100 €


def test_stop_still_triggers_inside_the_candle():
    r = run_backtest(candles(gap_open=100.0, low_at=5), settings(), "BTC/EUR", 86_400,
                     window=300, strategy=scripted(exit_at=999))
    out = sells(r["fills"])
    assert len(out) == 1 and out[0]["reason"] == "stop" and out[0]["ts_ms"] == 5 * D


def test_exit_signal_on_last_candle_sells_nothing():
    r = run_backtest(candles(n=EXIT_AT), settings(), "BTC/EUR", 86_400, window=300,
                     strategy=scripted())
    assert sells(r["fills"]) == [] and r["end_equity"] > 0


def test_portfolio_backtest_follows_the_same_rule(monkeypatch):
    monkeypatch.setitem(strategies.STRATEGIES, "scripted", scripted())
    r = run_portfolio({"BTC/EUR": candles()}, settings(), 86_400, ["scripted"], window=300, windows=1)
    out = sells(r["fills"])
    assert len(out) == 1 and out[0]["ts_ms"] == EXIT_AT * D
    assert out[0]["fill_price"] == pytest.approx(90.0 * (1 - 0.001))
