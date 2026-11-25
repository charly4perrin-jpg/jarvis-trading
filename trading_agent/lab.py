"""Laboratoire : compétition entre stratégies en paper trading.

Chaque stratégie reçoit son propre portefeuille fictif, séparé du portefeuille
principal, et joue sur les mêmes données réelles, avec le même moteur de risque.
On peut ainsi les comparer dans les mêmes conditions de marché.

Exécution simplifiée par rapport au portefeuille principal : prix de la dernière
clôture avec un glissement fixe (pas de lecture du carnet d'ordres), pour ne pas
multiplier les appels à la plateforme.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from trading_agent.config import Settings
from trading_agent.paper import PaperBroker
from trading_agent.regimes import can_enter
from trading_agent.risk import RiskManager
from trading_agent.strategies import LIVE_STRATEGIES as STRATEGIES


class Lab:
    def __init__(self, settings: Settings, directory: str):
        self.settings = settings
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.risk = RiskManager(settings)
        self.brokers = {
            name: PaperBroker(settings.paper_starting_cash, settings.paper_fee_rate,
                              settings.paper_slippage_bps,
                              str(self.dir / f"{name}_ledger.jsonl"),
                              str(self.dir / f"{name}_state.json"))
            for name in STRATEGIES}
        since = self.dir / "since.txt"
        if not since.exists():
            since.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        self.since = since.read_text(encoding="utf-8").strip()
        self.last_error: str | None = None

    def step(self, snap, market: dict) -> list[dict]:
        """Fait jouer chaque stratégie sur la dernière bougie close.
        Retourne les ordres fictifs exécutés pendant ce tour."""
        key = f"{snap.exchange}:{snap.symbol}:{snap.timeframe}"
        last_ts = snap.candles[-1][0]
        trades = []
        for name, strat in STRATEGIES.items():
            broker = self.brokers[name]
            broker.mark({snap.symbol: snap.last_price})
            result = None
            if snap.symbol in broker.portfolio.positions:
                result = broker.check_stops(snap.symbol, snap.candles)
                if not result and strat.exit(snap.candles):
                    result = broker.close_position(snap.symbol, snap.last_price, reason="exit_signal")
            elif broker.portfolio.analysed.get(key) != last_ts:
                broker.portfolio.analysed[key] = last_ts
                signal = strat.entry(snap.symbol, snap.candles)
                if signal and self.settings.regime_filter:
                    signal = signal if can_enter(strat, getattr(snap, "regime", None))[0] else None
                    if signal and self.settings.hmm_filter:   # même veto de Markov que le direct
                        from trading_agent.hmm_regime import hmm_blocks
                        signal = None if hmm_blocks(strat, getattr(snap, "hmm", None)) else signal
                if signal:
                    decision = self.risk.evaluate(signal, broker.portfolio, market)
                    if decision.approved:
                        result = broker.buy(decision.order, candle_ms=last_ts)
                broker.mark({})
            if result and result.get("status") == "filled":
                trades.append({"strategy": name, "label": strat.label, **result})
        return trades

    def standings(self, active: str) -> list[dict]:
        start = self.settings.paper_starting_cash
        rows = []
        for name, strat in STRATEGIES.items():
            portfolio = self.brokers[name].portfolio
            sells = []
            ledger = self.dir / f"{name}_ledger.jsonl"
            if ledger.exists():
                with ledger.open(encoding="utf-8") as f:
                    fills = [json.loads(line) for line in f if line.strip()]
                sells = [x for x in fills if x.get("side") == "sell"]
            wins = sum(1 for x in sells if x.get("realized_pnl", 0) > 0)
            rows.append({
                "name": name, "label": strat.label, "family": strat.family,
                "active": name == active, "equity": portfolio.equity,
                "return": portfolio.equity / start - 1,
                "trades": len(sells), "wins": wins,
                "in_position": bool(portfolio.positions),
            })
        return sorted(rows, key=lambda r: r["equity"], reverse=True)
