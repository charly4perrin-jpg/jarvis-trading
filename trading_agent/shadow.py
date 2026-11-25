"""Mode Shadow : ce qui se serait passé sans les protections.

Deux comparaisons, sans aucune transaction :
1. Signaux écartés. Chaque signal d'achat bloqué (régime, pause, contradicteur,
   risque) devient un trade hypothétique : entrée au prix du signal, stop prévu,
   objectif à 2 fois le risque, frais et glissement déduits. Il est suivi bougie par
   bougie jusqu'au stop, à l'objectif, ou 60 bougies au plus. Si une même bougie
   touche les deux, on retient le stop (hypothèse prudente).
   Bilan négatif = les protections ont évité des pertes ; positif = elles ont coûté.
2. Référence passive : acheter dès le départ les paires suivies, à parts égales, et
   ne plus rien faire. Jarvis doit faire mieux, ajusté au risque, pour être utile.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

MAX_CANDLES = 60


class ShadowBook:
    def __init__(self, path: str, fee_rate: float, slippage_bps: float, reward_risk: float = 2.0):
        self.path = Path(path)
        self.cost = 2 * (fee_rate + slippage_bps / 10_000)    # aller-retour, en fraction du prix
        self.reward_risk = reward_risk
        self.data = {"open": [], "closed": [], "reference": None}
        if self.path.exists():
            self.data = {**self.data, **json.loads(self.path.read_text(encoding="utf-8"))}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def record(self, signal, blocked_by: str, reason: str, candle_ms: int, corr: str) -> None:
        entry, stop = signal.reference_price, signal.stop_price
        if stop >= entry:
            return
        self.data["open"].append({
            "symbol": signal.symbol, "strategy": signal.strategy, "blocked_by": blocked_by,
            "reason": reason, "entry": entry, "stop": stop,
            "target": entry + self.reward_risk * (entry - stop), "opened_ms": candle_ms,
            "checked_ms": candle_ms, "candles": 0, "correlation_id": corr})
        self._save()

    def update(self, symbol: str, candles: list) -> list[dict]:
        closed = []
        for t in list(self.data["open"]):
            if t["symbol"] != symbol:
                continue
            for ts, open_, high, low, close, *_ in candles:
                if ts <= t["checked_ms"]:
                    continue
                t["checked_ms"] = ts
                t["candles"] += 1
                exit_, why = None, None
                if float(low) <= t["stop"]:
                    exit_, why = min(float(open_), t["stop"]), "stop"
                elif float(high) >= t["target"]:
                    exit_, why = max(float(open_), t["target"]), "objectif"
                elif t["candles"] >= MAX_CANDLES:
                    exit_, why = float(close), "durée maximale"
                if exit_ is not None:
                    risk = t["entry"] - t["stop"]
                    pnl = exit_ - t["entry"] - t["entry"] * self.cost
                    t.update({"exit": exit_, "exit_reason": why, "r": pnl / risk,
                              "closed_at": datetime.now(timezone.utc).isoformat()})
                    self.data["open"].remove(t)
                    self.data["closed"].append(t)
                    closed.append(t)
                    break
        if closed or any(t["symbol"] == symbol for t in self.data["open"]):
            self._save()
        return closed

    def mark_reference(self, watchlist: list[str], closes: dict, equity: float) -> dict | None:
        """Valeur de la référence « acheter et garder » à parts égales."""
        prices = {s: closes[s][-1][1] for s in watchlist if closes.get(s)}
        if not prices:
            return None
        ref = self.data["reference"]
        if ref is None:
            ref = self.data["reference"] = {"since": datetime.now(timezone.utc).isoformat(),
                                            "start_equity": equity, "prices": prices}
            self._save()
        common = [s for s in ref["prices"] if s in prices]
        if not common:
            return None
        growth = sum(prices[s] / ref["prices"][s] for s in common) / len(common)
        return {"since": ref["since"], "symbols": common, "start_equity": ref["start_equity"],
                "value": ref["start_equity"] * growth, "return": growth - 1}

    def summary(self) -> dict:
        groups: dict[str, dict] = {}
        for t in self.data["closed"] + self.data["open"]:
            g = groups.setdefault(t["blocked_by"], {"signals": 0, "resolved": 0, "wins": 0, "total_r": 0.0})
            g["signals"] += 1
            if "r" in t:
                g["resolved"] += 1
                g["wins"] += t["r"] > 0
                g["total_r"] += t["r"]
        for g in groups.values():
            g["average_r"] = g["total_r"] / g["resolved"] if g["resolved"] else None
        return {"by_blocker": groups, "open": len(self.data["open"]), "closed": len(self.data["closed"]),
                "recent": self.data["closed"][-20:][::-1]}
