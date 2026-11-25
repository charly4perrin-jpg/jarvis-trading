"""Propriétés du moteur de risque, vérifiées sur des milliers de situations tirées au hasard.

Hypothesis cherche activement un contre-exemple ; s'il en trouve un, il le réduit au cas
le plus simple et l'affiche. Ces règles ne doivent JAMAIS être violées.
"""
import math
from datetime import datetime, timedelta, timezone

from hypothesis import HealthCheck, given, settings as hsettings, strategies as st

from trading_agent.config import Settings
from trading_agent.models import PaperPortfolio, Side, Signal
from trading_agent.risk import RiskManager

S = Settings(_env_file=None)
RISK = RiskManager(S)
EPS = 1e-6
FIN = dict(allow_nan=False, allow_infinity=False)
CHECKS = hsettings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def costs(order, price, stop):
    slip, fee = S.paper_slippage_bps / 10_000, S.paper_fee_rate
    entry, exit_ = price * (1 + slip), stop * (1 - slip)
    return order.quantity * ((entry - exit_) + fee * (entry + exit_)), order.quantity * entry * (1 + fee)


@st.composite
def portfolios(draw):
    cash = draw(st.floats(0, 50_000, **FIN))
    positions = {}
    for i in range(draw(st.integers(0, 6))):
        mark = draw(st.floats(0.01, 100_000, **FIN))
        positions[f"P{i}/EUR"] = {"quantity": draw(st.floats(1e-6, 10, **FIN)), "entry_price": mark,
                                  "mark_price": mark, "stop_price": mark * draw(st.floats(0.5, 0.99)),
                                  "entry_fee": 0.0, "checked_ms": 0, "strategy": "x"}
    pending = {}
    for i in range(draw(st.integers(0, 2))):
        reserved = draw(st.floats(0, cash, **FIN)) if cash else 0.0
        pending[f"o{i}"] = {"symbol": f"Q{i}/EUR", "quantity": 1.0, "limit_price": 1.0, "stop_price": 0.5,
                            "reserved": reserved}
    equity = cash + sum(p["quantity"] * p["mark_price"] for p in positions.values())
    p = PaperPortfolio(cash=cash, equity=equity, positions=positions)
    p.pending = pending
    p.daily_pnl = draw(st.floats(-0.05, 0.05, **FIN)) * equity
    p.weekly_pnl = draw(st.floats(-0.1, 0.1, **FIN)) * equity
    p.consecutive_losses = draw(st.integers(0, 4))
    return p


@st.composite
def signals(draw):
    price = draw(st.floats(0.001, 200_000, **FIN))
    stop = price * draw(st.floats(0.5, 0.999, **FIN))
    return Signal(draw(st.sampled_from(["BTC/EUR", "ETH/EUR", "P0/EUR"])), Side.BUY,
                  draw(st.floats(0.7, 1.0, **FIN)), price, stop, "aléatoire", datetime.now(timezone.utc))


@st.composite
def markets(draw):
    return {"data_age_s": draw(st.floats(0, 100, **FIN)), "max_age_s": 100, "spread_bps": draw(st.floats(0, 40, **FIN)),
            "min_amount": draw(st.sampled_from([None, 1e-5, 0.01])), "min_cost": draw(st.sampled_from([None, 5.0])),
            "correlated_open_risk": draw(st.floats(0, 100, **FIN))}


@CHECKS
@given(portfolios(), signals(), markets())
def test_an_approved_order_never_breaks_a_limit(p, sig, market):
    d = RISK.evaluate(sig, p, market)
    if not d.approved:
        return
    o = d.order
    assert math.isfinite(o.quantity) and o.quantity > 0
    loss, cost = costs(o, sig.reference_price, sig.stop_price)
    assert loss <= p.equity * S.risk_per_trade_pct * (1 + 1e-9) + EPS                 # 0,5 % au plus
    reserved = sum(x["reserved"] for x in p.pending.values())
    assert cost <= max(0.0, p.cash - reserved - p.equity * S.min_cash_reserve_pct) + EPS  # réserve de cash
    exposure = sum(x["quantity"] * x["mark_price"] for x in p.positions.values()) + reserved
    assert exposure + o.quantity * sig.reference_price <= p.equity * S.max_total_exposure_pct * (1 + 1e-9) + EPS
    assert market["correlated_open_risk"] + loss <= p.equity * S.max_correlated_risk_pct * (1 + 1e-9) + EPS
    assert len(p.positions) + len(p.pending) < S.max_open_positions
    if market["min_cost"]:
        assert o.quantity * sig.reference_price >= market["min_cost"] - EPS


BREAKERS = {
    "perte du jour": lambda p, m, s: setattr(p, "daily_pnl", -p.equity * S.max_daily_loss_pct - 1),
    "perte de la semaine": lambda p, m, s: setattr(p, "weekly_pnl", -p.equity * S.max_weekly_loss_pct - 1),
    "pertes d'affilée": lambda p, m, s: setattr(p, "consecutive_losses", S.max_consecutive_losses),
    "trop de positions": lambda p, m, s: p.positions.update({f"Z{i}/EUR": {"quantity": 1e-9, "mark_price": 1.0,
                                                             "stop_price": 0.5} for i in range(S.max_open_positions)}),
    "données périmées": lambda p, m, s: m.update(data_age_s=m["max_age_s"] + 1),
    "spread trop large": lambda p, m, s: m.update(spread_bps=S.max_spread_bps + 1),
    "spread inconnu": lambda p, m, s: m.update(spread_bps=None),
    "risque corrélé épuisé": lambda p, m, s: m.update(correlated_open_risk=p.equity),
    "vitesse de perte": lambda p, m, s: setattr(p, "equity_marks", [
        [(datetime(2026, 1, 1, tzinfo=timezone.utc) - timedelta(hours=1)).isoformat(), p.equity * 1.1],
        [datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(), p.equity]]),
}


@CHECKS
@given(portfolios(), signals(), markets(), st.sampled_from(sorted(BREAKERS)))
def test_a_broken_rule_always_means_refusal(p, sig, market, rule):
    BREAKERS[rule](p, market, sig)
    assert not RISK.evaluate(sig, p, market).approved, rule


@CHECKS
@given(portfolios(), signals(), markets(), st.sampled_from(["prix", "stop", "confiance", "capital", "cash", "âge"]),
       st.sampled_from([math.nan, math.inf, -math.inf, -1.0, 0.0]))
def test_absurd_values_fail_closed(p, sig, market, field, bad):
    from dataclasses import replace
    if field == "prix":
        sig = replace(sig, reference_price=bad)
    elif field == "stop":
        sig = replace(sig, stop_price=bad)
    elif field == "confiance":
        sig = replace(sig, confidence=bad)
    elif field == "capital":
        p.equity = bad
    elif field == "cash":
        p.cash = bad
    else:
        market["data_age_s"] = bad
    d = RISK.evaluate(sig, p, market)                 # ne doit jamais lever d'exception
    if d.approved:                                    # et ne jamais accepter un ordre absurde
        assert math.isfinite(d.order.quantity) and d.order.quantity > 0
        assert all(math.isfinite(x) and x > 0 for x in (sig.reference_price, sig.stop_price, p.equity))
        assert math.isfinite(p.cash) and p.cash >= 0


@CHECKS
@given(portfolios(), signals(), markets())
def test_sell_orders_and_unvalidated_currencies_are_always_refused(p, sig, market):
    from dataclasses import replace
    assert not RISK.evaluate(replace(sig, side=Side.SELL), p, market).approved
    assert not RISK.evaluate(replace(sig, symbol="BTC/USDT"), p, market).approved


# ---------- simulateur : la comptabilité reste juste, quoi qu'il arrive ----------
from trading_agent.models import PaperOrder  # noqa: E402
from trading_agent.paper import PaperBroker  # noqa: E402

actions = st.lists(st.one_of(
    st.tuples(st.just("achat"), st.sampled_from(["A/EUR", "B/EUR", "C/EUR"]), st.floats(0.01, 1000, **FIN),
              st.floats(0.0001, 50, **FIN), st.floats(0.5, 0.99, **FIN)),
    st.tuples(st.just("vente"), st.sampled_from(["A/EUR", "B/EUR", "C/EUR"]), st.floats(0.01, 1000, **FIN)),
    st.tuples(st.just("prix"), st.sampled_from(["A/EUR", "B/EUR", "C/EUR"]), st.floats(0.01, 1000, **FIN)),
    st.tuples(st.just("bougie"), st.sampled_from(["A/EUR", "B/EUR", "C/EUR"]), st.floats(0.01, 1000, **FIN)),
), max_size=40)


@CHECKS
@given(actions)
def test_simulated_accounting_always_balances(steps):
    broker = PaperBroker(3000.0, 0.0025, 10, None, None)
    t = 0
    for step in steps:
        kind, sym, price = step[0], step[1], step[2]
        if kind == "achat":
            broker.buy(PaperOrder(sym, Side.BUY, step[3], price, price * step[4]), candle_ms=t)
        elif kind == "vente":
            broker.close_position(sym, price, reason="test")
        elif kind == "prix":
            broker.mark({sym: price})
        else:
            t += 60_000
            broker.check_stops(sym, [[t, price, price * 1.01, price * 0.97, price, 1.0]])
        p = broker.portfolio
        assert p.cash >= -EPS                                            # jamais de cash négatif
        assert all(x["quantity"] > 0 for x in p.positions.values())
        value = p.cash + sum(x["quantity"] * x["mark_price"] for x in p.positions.values())
        assert math.isclose(p.equity, value, rel_tol=1e-9, abs_tol=1e-6)  # capital = cash + positions
