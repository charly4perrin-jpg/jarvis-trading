import json
import math
import os
import uuid
from datetime import datetime, timedelta
from dataclasses import asdict
from pathlib import Path
from trading_agent.models import PaperPortfolio, PaperOrder, utc_now

def walk_book(levels: list, quantity: float, limit_price: float, buying: bool):
    """Consomme le carnet niveau par niveau sans dépasser `limit_price`.
    Retourne (quantité exécutée, prix moyen obtenu)."""
    filled = cost = 0.0
    for price, amount, *_ in levels:
        if (buying and price > limit_price) or (not buying and price < limit_price):
            break
        take = min(float(amount), quantity - filled)
        filled += take
        cost += take * float(price)
        if filled >= quantity:
            break
    return filled, (cost / filled if filled else 0.0)


class PaperBroker:
    def __init__(self, starting_cash: float, fee_rate: float, slippage_bps: float,
                 ledger_path: str | None, state_path: str | None = None,
                 max_slippage_bps: float = 50):
        self.state_path = Path(state_path) if state_path else None
        if self.state_path and self.state_path.exists():
            # Reprise après redémarrage : on recharge le portefeuille sauvegardé.
            self.portfolio = PaperPortfolio(**json.loads(self.state_path.read_text(encoding="utf-8")))
        else:
            self.portfolio = PaperPortfolio(cash=starting_cash, positions={}, equity=starting_cash)
        self.fee_rate = fee_rate
        self.slippage = slippage_bps / 10_000
        self.max_slippage = max_slippage_bps / 10_000
        self.fixed_now = None  # horloge simulée (backtest, tests) ; None = heure réelle
        self.path = Path(ledger_path) if ledger_path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def _record(self, event):
        if not self.path:
            return
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")

    def _save(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.portfolio)), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _now(self):
        return self.fixed_now or utc_now()

    def mark(self, prices: dict[str, float], now=None):
        p = self.portfolio
        position_value = 0.0
        for symbol, position in p.positions.items():
            if symbol in prices:
                position["mark_price"] = prices[symbol]
            position_value += position["quantity"] * position["mark_price"]
        p.equity = p.cash + position_value
        # Pertes jour/semaine = variation du capital depuis le début de la période
        # (frais et pertes latentes inclus). Remise à zéro à chaque nouveau jour/semaine UTC.
        if now:
            self.fixed_now = now
        now = self.fixed_now or utc_now()
        iso = now.isocalendar()
        day_key, week_key = now.date().isoformat(), f"{iso.year}-W{iso.week:02d}"
        if p.day_key != day_key:
            p.day_key, p.day_start_equity = day_key, p.equity
            p.consecutive_losses = 0  # nouveau jour : le compteur de pertes repart
        if p.week_key != week_key:
            p.week_key, p.week_start_equity = week_key, p.equity
        p.daily_pnl = p.equity - p.day_start_equity
        p.weekly_pnl = p.equity - p.week_start_equity
        stamp = now.isoformat()
        if p.equity_marks and (now - datetime.fromisoformat(p.equity_marks[-1][0])).total_seconds() < 60:
            p.equity_marks[-1] = [stamp, p.equity]       # un relevé par minute au plus
        else:
            p.equity_marks.append([stamp, p.equity])
        horizon = now - timedelta(hours=48)
        while p.equity_marks and datetime.fromisoformat(p.equity_marks[0][0]) < horizon:
            p.equity_marks.pop(0)
        self._save()

    def check_stops(self, symbol: str, candles: list):
        """Ferme la position si une bougie close, non encore vérifiée, a touché le stop."""
        position = self.portfolio.positions.get(symbol)
        if not position:
            return None
        for ts, open_, _high, low, _close, *_ in candles:
            if ts <= position.get("checked_ms", 0):
                continue
            position["checked_ms"] = ts
            if float(low) <= position["stop_price"]:
                # Si le marché ouvre sous le stop, on est exécuté à l'ouverture, pas au stop.
                return self.close_position(symbol, min(float(open_), position["stop_price"]), reason="stop")
        self._save()
        return None

    def reserved(self) -> float:
        return sum(o["reserved"] for o in self.portfolio.pending.values())

    def place_limit(self, order: PaperOrder, limit_price: float, placed_ms: int, expires_ms: int,
                    maker_fee: float, proposal_id: str | None = None, max_participation: float = 0.10) -> dict:
        """Ordre limite d'achat : il attend que le marché descende à `limit_price`.
        Il ne s'exécute que sur une bougie close après sa pose qui passe SOUS ce prix (le
        simple contact ne suffit pas : prudence), et seulement pour une part du volume
        échangé sous la limite (`max_participation`) : il peut donc s'exécuter en plusieurs
        fois. Le reste expire."""
        if not (math.isfinite(max_participation) and 0 < max_participation <= 1):
            return {"status": "rejected", "reason": "Invalid participation cap."}
        p = self.portfolio
        if order.symbol in p.positions or any(o["symbol"] == order.symbol for o in p.pending.values()):
            return {"status": "rejected", "reason": "Position or pending order already open in this symbol."}
        reserve = order.quantity * limit_price * (1 + maker_fee)
        if reserve > p.cash - self.reserved():
            return {"status": "rejected", "reason": "Insufficient simulated cash."}
        order_id = uuid.uuid4().hex[:10]
        p.pending[order_id] = {
            "order_id": order_id, "symbol": order.symbol, "quantity": order.quantity,
            "limit_price": limit_price, "stop_price": order.stop_price, "strategy": order.strategy,
            "reserved": reserve, "maker_fee": maker_fee, "placed_ms": placed_ms,
            "checked_ms": placed_ms, "expires_ms": expires_ms, "proposal_id": proposal_id,
            # `quantity` = reste à exécuter (compté par le risque) ; `requested_quantity` = demande initiale.
            "requested_quantity": order.quantity, "filled_quantity": 0.0,
            "max_participation": max_participation}
        event = {"type": "paper_order", "status": "placed", "ts": self._now().isoformat(),
                 **p.pending[order_id]}
        self._record(event)
        self._save()
        return {"status": "placed", **p.pending[order_id]}

    def cancel(self, order_id: str, reason: str) -> dict | None:
        order = self.portfolio.pending.pop(order_id, None)
        if order is None:
            return None
        event = {"type": "paper_order", "status": "cancelled", "reason": reason,
                 "ts": self._now().isoformat(), **order}
        self._record(event)
        self._save()
        return {**event, "status": "cancelled"}

    def check_pending(self, symbol: str, candles: list) -> list[dict]:
        """Fait avancer les ordres limite de cette paire sur les bougies closes."""
        results = []
        for order_id, o in list(self.portfolio.pending.items()):
            if o["symbol"] != symbol:
                continue
            # La partie déjà achetée a été vendue (stop) : on ne rachète pas, le reste est annulé.
            if o.get("filled_quantity", 0) > 0 and symbol not in self.portfolio.positions:
                results.append(self.cancel(order_id, "position_closed"))
                continue
            for candle in candles:
                ts, open_, high, low = candle[0], float(candle[1]), float(candle[2]), float(candle[3])
                if ts <= o["checked_ms"]:
                    continue
                o["checked_ms"] = ts
                if low < o["limit_price"]:
                    quantity = self._limit_quantity(o, candle)
                    if quantity > 0:
                        results.append(self._fill_limit(order_id, quantity, min(open_, o["limit_price"]), ts))
                        if order_id not in self.portfolio.pending:
                            break
                if ts >= o["expires_ms"]:
                    results.append(self.cancel(order_id, "expired"))
                    break
        self._save()
        return results

    @staticmethod
    def _limit_quantity(o: dict, candle: list) -> float:
        """Quantité exécutable sur cette bougie. Faute du détail des transactions, le volume
        est supposé réparti uniformément entre le plus bas et le plus haut ; l'ordre n'en
        prend qu'une part. Volume absent ou invalide : rien (prudence)."""
        try:
            high, low, volume = float(candle[2]), float(candle[3]), float(candle[5])
        except (IndexError, TypeError, ValueError):
            return 0.0
        if not all(math.isfinite(x) for x in (high, low, volume)) or volume <= 0 or high < low:
            return 0.0
        below = volume if high == low else volume * min(1.0, (o["limit_price"] - low) / (high - low))
        cap = o.get("max_participation", 0.10) * below
        quantity = min(o["quantity"], cap)
        return quantity if math.isfinite(quantity) and quantity > 0 else 0.0

    def _fill_limit(self, order_id: str, quantity: float, price: float, candle_ms: int) -> dict:
        p = self.portfolio
        o = p.pending[order_id]
        gross = quantity * price
        fee = gross * o["maker_fee"]
        p.cash -= gross + fee
        held = p.positions.get(o["symbol"])
        if held:   # exécution suivante d'un même ordre : prix moyen et frais cumulés
            total = held["quantity"] + quantity
            held["entry_price"] = (held["quantity"] * held["entry_price"] + gross) / total
            held["quantity"] = total
            held["entry_fee"] += fee
        else:
            # Le stop est vérifié dès la bougie d'exécution (checked_ms juste avant) : si
            # elle est aussi descendue sous le stop, on suppose le pire ordre des événements.
            p.positions[o["symbol"]] = {
                "quantity": quantity, "entry_price": price, "mark_price": price,
                "stop_price": o["stop_price"], "entry_fee": fee, "checked_ms": candle_ms - 1,
                "strategy": o["strategy"], "proposal_id": o["proposal_id"]}
        requested = o.get("requested_quantity", o["quantity"])
        o["filled_quantity"] = o.get("filled_quantity", 0.0) + quantity
        remaining = o["quantity"] - quantity
        if remaining <= requested * 1e-9:
            remaining = 0.0
            p.pending.pop(order_id)
        else:
            o["quantity"] = remaining
            o["reserved"] = remaining * o["limit_price"] * (1 + o["maker_fee"])
        self.mark({})
        event = {"type": "paper_fill", "symbol": o["symbol"], "side": "buy", "order_type": "limit",
                 "strategy": o["strategy"], "ts": self._now().isoformat(), "quantity": quantity,
                 "requested_quantity": requested, "filled_quantity": o["filled_quantity"],
                 "remaining_quantity": remaining, "partial": o["filled_quantity"] < requested * (1 - 1e-9),
                 "fill_price": price, "slippage_bps": (price / o["limit_price"] - 1) * 10_000, "fee": fee,
                 "cash_after": p.cash, "order_id": order_id, "proposal_id": o["proposal_id"]}
        self._record(event)
        return {"status": "filled", **event}

    def buy(self, order: PaperOrder, candle_ms: int = 0, asks: list | None = None):
        """Achat simulé. Avec `asks` (carnet réel), le prix est celui obtenu en
        consommant le carnet ; l'exécution peut être partielle."""
        if order.symbol in self.portfolio.positions:
            return {"status": "rejected", "reason": "Position already open in this symbol."}
        quantity, fill_price = order.quantity, order.reference_price * (1 + self.slippage)
        if asks is not None:
            quantity, fill_price = walk_book(
                asks, order.quantity, order.reference_price * (1 + self.max_slippage), buying=True)
            if quantity <= 0:
                return {"status": "rejected",
                        "reason": "No liquidity within the allowed slippage (price moved away)."}
        gross = fill_price * quantity
        fee = gross * self.fee_rate
        total = gross + fee
        if total > self.portfolio.cash - self.reserved():
            return {"status": "rejected", "reason": "Insufficient simulated cash."}
        self.portfolio.cash -= total
        self.portfolio.positions[order.symbol] = {
            "quantity": quantity, "entry_price": fill_price,
            "mark_price": fill_price, "stop_price": order.stop_price,
            "entry_fee": fee, "checked_ms": candle_ms, "strategy": order.strategy
        }
        self.mark({})
        event = {
            "type": "paper_fill", "symbol": order.symbol, "side": "buy",
            "strategy": order.strategy, "ts": self._now().isoformat(),
            "quantity": quantity, "requested_quantity": order.quantity,
            "partial": quantity < order.quantity * (1 - 1e-9), "fill_price": fill_price,
            "slippage_bps": (fill_price / order.reference_price - 1) * 10_000,
            "fee": fee, "cash_after": self.portfolio.cash,
            # Paramètres exacts du calcul, pour rejouer l'exécution sur le carnet enregistré.
            "reference_price": order.reference_price, "fee_rate": self.fee_rate,
            "max_slippage": self.max_slippage, "slippage": self.slippage, "used_book": asks is not None
        }
        self._record(event)
        return {"status": "filled", **event}

    def close_position(self, symbol: str, reference_price: float, reason: str = "manual",
                       bids: list | None = None):
        """Vente simulée. Avec `bids` (carnet réel), la vente peut être partielle :
        le reste de la position est alors conservé."""
        position = self.portfolio.positions.get(symbol)
        if not position:
            return {"status": "rejected", "reason": "No open position."}
        held = position["quantity"]
        quantity, fill_price = held, reference_price * (1 - self.slippage)
        if bids is not None:
            quantity, fill_price = walk_book(
                bids, held, reference_price * (1 - self.max_slippage), buying=False)
            if quantity <= 0:
                return {"status": "rejected",
                        "reason": "No liquidity within the allowed slippage (price moved away)."}
        share = quantity / held
        entry_fee = position["entry_fee"] * share
        gross = fill_price * quantity
        fee = gross * self.fee_rate
        proceeds = gross - fee
        pnl = proceeds - quantity * position["entry_price"] - entry_fee
        self.portfolio.cash += proceeds
        self.portfolio.consecutive_losses = self.portfolio.consecutive_losses + 1 if pnl < 0 else 0
        partial = share < 1 - 1e-9
        if partial:
            position["quantity"] -= quantity
            position["entry_fee"] -= entry_fee
        else:
            del self.portfolio.positions[symbol]
        self.mark({})
        event = {"type": "paper_fill", "symbol": symbol, "side": "sell",
                 "strategy": position.get("strategy"), "ts": self._now().isoformat(),
                 "quantity": quantity, "partial": partial, "fill_price": fill_price,
                 "slippage_bps": (1 - fill_price / reference_price) * 10_000,
                 "fee": fee, "entry_fee": entry_fee, "realized_pnl": pnl, "reason": reason,
                 # Lien avec l'achat : la proposition d'origine et le prix d'entrée.
                 "proposal_id": position.get("proposal_id"), "entry_price": position.get("entry_price"),
                 "cash_after": self.portfolio.cash,
                 # Paramètres exacts du calcul, pour rejouer l'exécution sur le carnet enregistré.
                 "reference_price": reference_price, "requested_quantity": held, "fee_rate": self.fee_rate,
                 "max_slippage": self.max_slippage, "slippage": self.slippage, "used_book": bids is not None}
        self._record(event)
        return {"status": "filled", **event}
