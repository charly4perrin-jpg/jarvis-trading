import math
from datetime import datetime, timedelta
from dataclasses import dataclass
from trading_agent.config import Settings
from trading_agent.models import PaperOrder, PaperPortfolio, Signal, Side

@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    order: PaperOrder | None = None

class RiskManager:
    def __init__(self, settings: Settings):
        self.s = settings

    def evaluate(self, signal: Signal, portfolio: PaperPortfolio, market: dict | None = None):
        """`market` décrit l'état des données (fraîcheur, spread, minimums de la
        plateforme). S'il est fourni, toute information manquante vaut refus."""
        # Barrière ultime : un nombre non fini (NaN, infini) ou négatif là où il ne peut pas
        # l'être vient forcément d'un bug ou de données corrompues. Dans le doute : refus.
        if not _inputs_are_sane(signal, portfolio, market):
            return RiskDecision(False, "Invalid or non-finite number in inputs.")
        if signal.side != Side.BUY:
            return RiskDecision(False, "Spot-only starter: sell-to-open/shorting is disabled.")
        if signal.confidence < 0.70:
            return RiskDecision(False, "Signal confidence below threshold.")
        if signal.reference_price <= 0 or signal.stop_price <= 0 or signal.stop_price >= signal.reference_price:
            return RiskDecision(False, "Invalid buy price/stop relationship.")
        if portfolio.equity <= 0:
            return RiskDecision(False, "Portfolio equity is not positive.")
        if portfolio.daily_pnl <= -portfolio.equity * self.s.max_daily_loss_pct:
            return RiskDecision(False, "Daily loss limit reached.")
        if portfolio.weekly_pnl <= -portfolio.equity * self.s.max_weekly_loss_pct:
            return RiskDecision(False, "Weekly loss limit reached.")
        pending = getattr(portfolio, "pending", {}) or {}
        if any(o["symbol"] == signal.symbol for o in pending.values()):
            return RiskDecision(False, "Pending order already open for this symbol.")
        if len(portfolio.positions) + len(pending) >= self.s.max_open_positions:
            return RiskDecision(False, "Maximum open positions reached.")
        if portfolio.consecutive_losses >= self.s.max_consecutive_losses:
            return RiskDecision(False, "Consecutive loss limit reached.")
        marks = getattr(portfolio, "equity_marks", None) or []
        if marks:
            last = datetime.fromisoformat(marks[-1][0])
            since = last - timedelta(hours=self.s.loss_velocity_window_h)
            peak = max(e for ts, e in marks if datetime.fromisoformat(ts) >= since)
            if peak > 0 and portfolio.equity <= peak * (1 - self.s.max_loss_velocity_pct):
                return RiskDecision(False, "Loss velocity limit reached.")
        quote = signal.symbol.split("/")[-1]
        if quote not in self.s.allowed_quotes:
            return RiskDecision(False, "Quote currency not validated.")
        if market is not None:
            age, max_age = market.get("data_age_s"), market.get("max_age_s")
            if age is None or max_age is None or age < 0 or age > max_age:
                return RiskDecision(False, "Market data is stale or missing.")
            if "book_age_s" in market:   # carnet lu pour cette décision : il doit être frais
                book_age = market["book_age_s"]
                if book_age is None or book_age < -5 or book_age > self.s.max_book_age_s:   # -5 s : décalage d'horloge toléré
                    return RiskDecision(False, "Order book is stale or missing.")
            spread = market.get("spread_bps")
            if spread is None or spread > self.s.max_spread_bps:
                return RiskDecision(False, "Spread unknown or too wide.")

        # Perte réelle par unité si le stop est touché : écart de prix + glissement
        # à l'achat et à la vente + frais sur les deux ordres.
        slip = self.s.paper_slippage_bps / 10_000
        fee = self.s.paper_fee_rate
        entry = signal.reference_price * (1 + slip)
        exit_ = signal.stop_price * (1 - slip)
        loss_per_unit = (entry - exit_) + fee * (entry + exit_)
        cost_per_unit = entry * (1 + fee)
        risk_budget = portfolio.equity * self.s.risk_per_trade_pct
        quantity = risk_budget / loss_per_unit
        max_exposure = portfolio.equity * self.s.max_total_exposure_pct
        reserved = sum(o["reserved"] for o in pending.values())
        current_exposure = sum(p["quantity"] * p["mark_price"] for p in portfolio.positions.values()) + reserved
        max_notional = max(0.0, max_exposure - current_exposure)
        cash_reserve = portfolio.equity * self.s.min_cash_reserve_pct
        spendable = max(0.0, portfolio.cash - reserved - cash_reserve)
        notional = min(quantity * cost_per_unit, max_notional, spendable)
        quantity = notional / cost_per_unit
        if quantity <= 0:
            return RiskDecision(False, "No spendable cash or exposure capacity.")
        if quantity * loss_per_unit > risk_budget + 1e-8:
            return RiskDecision(False, "Position size exceeds risk budget.")
        if market is not None and "correlated_open_risk" in market:
            # Les positions corrélées sont UN seul risque : leur risque cumulé, plus celui
            # de la nouvelle position, reste sous la limite. Sinon on réduit, ou on refuse.
            room = portfolio.equity * self.s.max_correlated_risk_pct - market["correlated_open_risk"]
            # Une miette de budget ne justifie pas une position minuscule : refus en dessous
            # du quart du risque normal d'un trade.
            if room < 0.25 * risk_budget:
                return RiskDecision(False, "Correlated risk limit reached.")
            quantity = min(quantity, room / loss_per_unit)
        if market is not None:
            min_amount, min_cost = market.get("min_amount"), market.get("min_cost")
            if (min_amount and quantity < min_amount) or (
                    min_cost and quantity * signal.reference_price < min_cost):
                return RiskDecision(False, "Order below exchange minimum size.")
        if not (math.isfinite(quantity) and quantity > 0):
            return RiskDecision(False, "Invalid or non-finite number in inputs.")
        return RiskDecision(True, "Approved by paper risk rules.", PaperOrder(
            symbol=signal.symbol, side=Side.BUY, quantity=quantity,
            reference_price=signal.reference_price, stop_price=signal.stop_price,
            strategy=signal.strategy
        ))


def _finite(*values, positive: bool = False, allow_none: bool = False) -> bool:
    for v in values:
        if v is None and allow_none:
            continue
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            return False
        if positive and v <= 0:
            return False
    return True


def _inputs_are_sane(signal, portfolio, market) -> bool:
    if not _finite(signal.reference_price, signal.stop_price, positive=True):
        return False
    if not _finite(signal.confidence) or not 0 <= signal.confidence <= 1:
        return False
    if not _finite(portfolio.equity, portfolio.cash, portfolio.daily_pnl, portfolio.weekly_pnl):
        return False
    if portfolio.cash < 0:
        return False
    for p in portfolio.positions.values():
        if not _finite(p.get("quantity"), p.get("mark_price"), p.get("stop_price")) or p["quantity"] < 0:
            return False
    for o in (getattr(portfolio, "pending", None) or {}).values():
        if not _finite(o.get("reserved")) or o["reserved"] < 0:
            return False
    if market is not None:
        if not _finite(market.get("data_age_s"), market.get("max_age_s"), market.get("spread_bps"),
                       market.get("book_age_s"),
                       market.get("min_amount"), market.get("min_cost"), market.get("correlated_open_risk"),
                       allow_none=True):
            return False
        if (market.get("correlated_open_risk") or 0) < 0:
            return False
    return True

