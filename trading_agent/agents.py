"""Les agents et le cycle coordonné.

Les agents sont du code déterministe (aucun modèle de langage). Chacun enveloppe
un module existant, n'émet que ses propres types d'événements (voir
`events.PERMISSIONS`) et décrit en français ce qu'il vient de faire dans le
champ `result` de l'événement.

Parcours obligatoire d'un achat :
market_intelligence (scanner, données) -> regime_detector -> strategy_analyst
-> adversarial_analyst -> trade_proposer (Trade Manager) -> risk_manager
-> execution -> portfolio_manager -> auditor.
L'agent d'exécution refuse tout achat dont la proposition n'a pas été validée par
le Risk Manager, a été refusée, a déjà été exécutée ou a expiré. Les ventes (stop, signal de sortie) réduisent le risque et ne
demandent pas cette validation.
"""
import asyncio
import json
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

from trading_agent.config import Settings
from trading_agent.events import Event, EventBus, read_events, verify_chain
from trading_agent.exchanges import PublicExchange
from trading_agent.market_scanner import MarketScanner
from trading_agent.models import Signal, utc_now
from trading_agent.reconcile import fingerprint
from trading_agent.adversarial import data_problems, objections as adversarial_objections  # noqa: F401
from trading_agent.paper import PaperBroker, walk_book
from trading_agent.performance import PauseList, analyse, load_fills
from trading_agent.shadow import ShadowBook
from trading_agent.learning import ParamStore, effective
from trading_agent.regimes import can_enter, classify, describe
from trading_agent.risk import RiskDecision, RiskManager
from trading_agent.runner import closed_candles, seconds_to_next_candle
from trading_agent.strategies import STRATEGIES, get_strategy, rsi

STABLECOINS = {"USDT", "USDC", "EURC", "EURT", "DAI", "TUSD", "PYUSD", "FDUSD", "USDE", "EURS"}
MIN_SCAN_DEPTH_EUR = 20_000   # profondeur minimale à ±0,5 % pour suivre une paire

RISK_REASONS_FR = {
    "Invalid or non-finite number in inputs.": "donnée invalide (nombre indéfini, infini ou négatif) : refus par prudence",
    "Loss velocity limit reached.": "le capital a trop baissé sur les dernières 24 h (pause glissante)",
    "Pending order already open for this symbol.": "un ordre limite est déjà en attente sur cette paire",
    "Quote currency not validated.": "devise de cotation non validée (EUR uniquement pour l'instant)",
    "Correlated risk limit reached.": "limite de risque des positions corrélées atteinte",
    "Spot-only starter: sell-to-open/shorting is disabled.": "la vente à découvert est interdite (Spot uniquement)",
    "Signal confidence below threshold.": "confiance du signal sous le seuil",
    "Invalid buy price/stop relationship.": "prix d'entrée et stop incohérents",
    "Portfolio equity is not positive.": "capital nul ou négatif",
    "Daily loss limit reached.": "limite de perte journalière atteinte",
    "Weekly loss limit reached.": "limite de perte hebdomadaire atteinte",
    "Maximum open positions reached.": "nombre maximal de positions atteint",
    "No spendable cash or exposure capacity.": "plus de cash disponible ou exposition maximale atteinte",
    "Position size exceeds risk budget.": "taille de position supérieure au budget de risque",
    "Consecutive loss limit reached.": "trop de pertes consécutives aujourd'hui",
    "Market data is stale or missing.": "données de marché périmées ou manquantes",
    "Order book is stale or missing.": "carnet d'ordres périmé ou manquant",
    "Spread unknown or too wide.": "spread inconnu ou trop large",
    "Order below exchange minimum size.": "ordre inférieur au minimum de la plateforme",
}


def eur(value: float) -> str:
    return f"{value:,.2f} €".replace(",", " ").replace(".", ",")


@dataclass
class MarketSnapshot:
    exchange: str
    symbol: str
    timeframe: str
    candles: list
    last_price: float
    spread_bps: float | None
    data_age_s: float | None = None
    max_age_s: float | None = None
    min_amount: float | None = None
    min_cost: float | None = None
    regime: dict | None = None
    hmm: dict | None = None      # régime de Markov caché (information + veto « agité » confirmé)


class Agent:
    name = ""
    role = ""
    inputs: tuple = ()     # ce que l'agent reçoit
    outputs: tuple = ()    # types d'événements qu'il émet
    depends: tuple = ()    # agents dont il a besoin

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.status = "idle"        # idle | active | error | stopped
        self.enabled = True
        self.last_heartbeat = None
        self.last_error = None

    def emit(self, type_: str, target: str, corr: str, result: str,
             payload: dict | None = None, status: str = "ok",
             started: float | None = None, priority: str = "normal",
             reply_to: str | None = None) -> Event:
        self.last_heartbeat = utc_now()
        duration = (perf_counter() - started) * 1000 if started else None
        return self.bus.publish(Event(
            source=self.name, target=target, type=type_, correlation_id=corr,
            status=status, result=result, payload=payload or {}, duration_ms=duration,
            priority=priority, reply_to=reply_to))

    def describe(self) -> dict:
        return {"name": self.name, "role": self.role, "status": self.status,
                "inputs": list(self.inputs), "outputs": list(self.outputs),
                "depends": list(self.depends),
                "enabled": self.enabled, "last_heartbeat": self.last_heartbeat,
                "last_error": self.last_error}


class MarketIntelligence(Agent):
    name = "market_intelligence"
    role = "Collecte les prix, bougies et spreads publics et les transmet horodatés."
    inputs = ("plateforme : bougies, prix, carnet, liste des marchés",)
    outputs = ("market.snapshot", "market.order_book", "market.scan")

    async def collect(self, ex, exchange_id, symbol, timeframe, corr) -> MarketSnapshot:
        started = perf_counter()
        tf_ms = ex.timeframe_seconds(timeframe) * 1000
        now_ms = ex.now_ms()
        candles = closed_candles(await ex.ohlcv(symbol, timeframe=timeframe, limit=300), tf_ms, now_ms)
        if not candles:
            raise RuntimeError("aucune bougie close reçue")
        ticker = await ex.ticker(symbol) or {}
        bid, ask = ticker.get("bid"), ticker.get("ask")
        spread_bps = None
        if bid and ask and bid > 0 and ask >= bid:
            spread_bps = (ask - bid) / ((ask + bid) / 2) * 10_000
        last_price = float(candles[-1][4])
        data_age_s = (now_ms - candles[-1][0] - tf_ms) / 1000
        limits = ex.limits(symbol)
        spread_txt = f"{spread_bps:.1f} bps" if spread_bps is not None else "inconnu"
        self.emit("market.snapshot", "strategy_analyst", corr,
                  f"{symbol} sur {exchange_id} : dernière clôture {eur(last_price)}, "
                  f"spread {spread_txt}, {len(candles)} bougies closes.",
                  {"exchange": exchange_id, "symbol": symbol, "timeframe": timeframe,
                   "last_price": last_price, "spread_bps": spread_bps,
                   "candle_count": len(candles), "last_candle_ms": candles[-1][0],
                   # Pour rejouer la décision sur exactement les mêmes données (reconcile.py).
                   "first_candle_ms": candles[0][0], "candles_sha256": fingerprint(candles),
                   "data_age_s": data_age_s, **limits},
                  started=started)
        return MarketSnapshot(exchange_id, symbol, timeframe, candles, last_price, spread_bps,
                              data_age_s, tf_ms / 1000, limits.get("min_amount"), limits.get("min_cost"))


    async def scan(self, ex, exchange_id: str, main_symbol: str, size: int, corr: str,
                   keep: list[str] | None = None) -> list[str]:
        """Scanner de marchés : choisit les paires à suivre (la paire principale, puis
        les plus liquides au spread serré et au carnet profond). Stablecoins exclus."""
        started = perf_counter()
        candidates = await MarketScanner(order_book_markets=12).scan(ex, exchange_id)
        ranked = [c for c in candidates if c["symbol"].split("/")[0] not in STABLECOINS
                  and (c["ask_depth_eur_0_5pct"] or 0) >= MIN_SCAN_DEPTH_EUR]
        watchlist = [main_symbol] + [c["symbol"] for c in ranked if c["symbol"] != main_symbol]
        watchlist = watchlist[:max(1, size)]
        # Une paire où une position est ouverte reste toujours suivie (stops, sorties).
        watchlist += [s for s in keep or [] if s not in watchlist]
        top = [{k: c[k] for k in ("symbol", "quote_volume_24h", "spread_bps",
                                  "bid_depth_eur_0_5pct", "ask_depth_eur_0_5pct")} for c in ranked[:10]]
        self.emit("market.scan", "coordinator", corr,
                  f"Scanner : {len(candidates)} marchés liquides sur {exchange_id}, "
                  f"{len(watchlist)} suivis : {', '.join(watchlist)}.",
                  {"exchange": exchange_id, "watchlist": watchlist, "candidates": top},
                  started=started)
        return watchlist

    async def order_book(self, ex, symbol, corr, target: str = "execution") -> dict:
        """Carnet d'ordres lu juste avant une exécution : c'est lui qui fixe le prix simulé."""
        started = perf_counter()
        book = await ex.order_book(symbol, limit=50)
        read_ms = ex.now_ms()
        # Heure du carnet donnée par la plateforme si elle existe, sinon heure de réception.
        stamp = book.get("timestamp")
        book_ms = int(stamp) if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) else read_ms
        bids, asks = book.get("bids") or [], book.get("asks") or []
        if not bids or not asks:
            raise RuntimeError("carnet d'ordres vide")
        self.emit("market.order_book", target, corr,
                  f"Carnet {symbol} : meilleur achat {eur(bids[0][0])}, meilleure vente "
                  f"{eur(asks[0][0])}, {len(bids)} et {len(asks)} niveaux.",
                  {"symbol": symbol, "best_bid": bids[0][0], "best_ask": asks[0][0],
                   "bid_levels": len(bids), "ask_levels": len(asks),
                   # Carnet complet lu à la décision : permet de rejouer l'exécution (reconcile.py).
                   "bids": [list(level[:2]) for level in bids], "asks": [list(level[:2]) for level in asks],
                   "read_ms": read_ms, "book_ms": book_ms}, started=started)
        return {"bids": bids, "asks": asks, "book_ms": book_ms}


class RegimeDetector(Agent):
    name = "regime_detector"
    role = "Qualifie le marché (tendance, volatilité, chocs, volume) avec des indicateurs chiffrés."
    inputs = ("market.snapshot",)
    outputs = ("regime.detected",)
    depends = ("market_intelligence",)

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        self._hmm_cache: dict[tuple, dict | None] = {}

    def markov(self, snap: MarketSnapshot) -> dict | None:
        """Régime de Markov de la dernière bougie close, calculé une fois par bougie.
        Toute erreur donne None (lecture impossible), jamais une étiquette devinée."""
        from trading_agent.hmm_regime import hmm_regime
        if not snap.candles:
            return None
        key = (snap.exchange, snap.symbol, snap.timeframe, snap.candles[-1][0])
        if key not in self._hmm_cache:
            try:
                self._hmm_cache[key] = hmm_regime(snap.candles)
            except Exception:
                self._hmm_cache[key] = None
            if len(self._hmm_cache) > 500:
                self._hmm_cache.pop(next(iter(self._hmm_cache)))
        return self._hmm_cache[key]

    def detect(self, snap: MarketSnapshot, corr) -> dict | None:
        started = perf_counter()
        regime = classify(snap.candles)
        hmm = self.markov(snap)
        snap.hmm = hmm
        hmm_txt = ("Markov : lecture impossible" if hmm is None
                   else f"Markov : {hmm['state']}" + (f" (sûr à {hmm['prob']:.0%})" if hmm["confident"] else ""))
        allowed = {name: can_enter(strat, regime)[0] for name, strat in STRATEGIES.items()}
        stress = bool(regime and (regime["shock"] or (regime["volatility"] == "haute"
                                                      and regime["trend"] == "baissier")))
        self.emit("regime.detected", "strategy_analyst", corr,
                  f"Régime {snap.symbol} : {describe(regime)}"
                  + (" — situation de stress" if stress else "") + f" ; {hmm_txt}.",
                  {"symbol": snap.symbol, "regime": regime, "allowed": allowed, "stress": stress,
                   "hmm": hmm},
                  priority="high" if stress else "normal", started=started)
        snap.regime = regime
        return regime


class StrategyAnalyst(Agent):
    name = "strategy_analyst"
    role = "Cherche des signaux d'entrée et de sortie. Ne peut rien exécuter."
    inputs = ("market.snapshot", "regime.detected")
    outputs = ("strategy.signal", "strategy.exit_signal", "strategy.no_signal", "strategy.lab")
    depends = ("market_intelligence", "regime_detector")

    def __init__(self, bus: EventBus, strategy: str = "momentum_demo", regime_filter: bool = True,
                 pause_path: str | None = None, learning_dir: str | None = None,
                 hmm_filter: bool = True):
        super().__init__(bus)
        self.strategy = get_strategy(strategy)
        # Réglages validés par une personne (Learning Manager) ; d'origine sinon.
        self.params = ParamStore(learning_dir) if learning_dir else None
        self.regime_filter = regime_filter
        self.hmm_filter = hmm_filter
        self.pauses = PauseList(pause_path) if pause_path else None
        self.last_block = ("", "")   # (par quoi, pourquoi) le dernier signal a été écarté

    def analyze(self, snap: MarketSnapshot, holding: bool, corr,
                held_strategy: str | None = None) -> tuple[str, Signal | None]:
        started = perf_counter()
        if holding:
            # Une position se ferme avec la règle de la stratégie qui l'a ouverte.
            strategy = effective(held_strategy if held_strategy in STRATEGIES else self.strategy.name,
                                 self.params)
            if strategy.exit(snap.candles):
                self.emit("strategy.exit_signal", "execution", corr,
                          f"Signal de sortie ({strategy.label}) : {strategy.exit_reason}.",
                          {"symbol": snap.symbol, "strategy": strategy.name}, started=started)
                return "exit", None
            self.emit("strategy.no_signal", "coordinator", corr,
                      f"Position conservée : aucune raison de sortir ({strategy.label}).",
                      {"symbol": snap.symbol, "strategy": strategy.name}, started=started)
            return "none", None
        strategy = effective(self.strategy.name, self.params)
        signal = strategy.entry(snap.symbol, snap.candles)
        if signal is None:
            self.emit("strategy.no_signal", "coordinator", corr,
                      f"Aucun signal ({strategy.label}) : {strategy.no_signal}.",
                      {"symbol": snap.symbol, "strategy": strategy.name}, started=started)
            return "none", None
        paused = self.pauses.all().get(strategy.name) if self.pauses else None
        if paused:
            self.last_block = ("pause", paused["reason"])
            self.emit("strategy.no_signal", "coordinator", corr,
                      f"Signal d'achat ({strategy.label}) écarté : stratégie en pause depuis le "
                      f"{paused['since'][:10]} ({paused['reason']}). Seule une personne peut la relancer.",
                      {"symbol": snap.symbol, "strategy": strategy.name, "paused": True},
                      started=started)
            return "blocked", signal
        if self.regime_filter:
            allowed, why = can_enter(strategy, snap.regime)
            if not allowed:
                self.last_block = ("régime", why)
                self.emit("strategy.no_signal", "coordinator", corr,
                          f"Signal d'achat ({strategy.label}) écarté : {why}. Ne pas trader est "
                          f"une décision à part entière.",
                          {"symbol": snap.symbol, "strategy": strategy.name, "regime": snap.regime,
                           "regime_blocked": True}, started=started)
                return "blocked", signal
            hmm = snap.hmm
            if (self.hmm_filter and hmm and hmm.get("confident") and hmm.get("state") == "agité"
                    and not strategy.high_vol):
                why = f"modèle de Markov : phase agitée confirmée (probabilité {hmm['prob']:.0%})"
                self.last_block = ("régime", why)
                self.emit("strategy.no_signal", "coordinator", corr,
                          f"Signal d'achat ({strategy.label}) écarté : {why}, stratégie non prévue "
                          f"pour la forte volatilité.",
                          {"symbol": snap.symbol, "strategy": strategy.name, "hmm": hmm,
                           "regime_blocked": True, "hmm_blocked": True}, started=started)
                return "blocked", signal
        self.emit("strategy.signal", "trade_proposer", corr,
                  f"Signal d'achat ({strategy.label}) sur {snap.symbol} à "
                  f"{eur(signal.reference_price)}, confiance {signal.confidence:.0%}.",
                  {"symbol": signal.symbol, "side": signal.side.value,
                   "confidence": signal.confidence, "reference_price": signal.reference_price,
                   "stop_price": signal.stop_price, "strategy": strategy.name,
                   "rationale": signal.rationale}, started=started)
        return "signal", signal


class ExchangeMonitor(Agent):
    name = "exchange_monitor"
    role = "Surveille chaque plateforme (pannes, lenteur, limites, maintenance) et la cohérence des soldes."
    inputs = ("mesures des appels à la plateforme", "portefeuille")
    outputs = ("exchange.status", "anomaly.report")
    depends = ("market_intelligence", "portfolio_manager")

    def check(self, ex, exchange_id: str, portfolio, corr, news: list[str] | None = None) -> bool:
        """Vrai si l'on peut opérer sur cette plateforme pendant ce cycle."""
        started = perf_counter()
        health = getattr(ex, "health", None)
        snap = health.snapshot() if health else None
        blocking, warnings = [], []
        if snap:
            if snap["circuit"] == "open":
                blocking.append(f"coupe-circuit ouvert ({snap['last_error']})")
            if snap["maintenance"]:
                blocking.append("plateforme en maintenance")
            rate = snap["recent_error_rate"]
            if rate is not None and rate >= 0.5 and snap["calls"] >= 4:
                blocking.append(f"{rate:.0%} des derniers appels en échec")
            elif rate:
                warnings.append(f"{rate:.0%} des derniers appels en échec")
            if snap["latency_ms"] and snap["latency_ms"] > 5_000:
                warnings.append(f"réponses lentes ({snap['latency_ms'] / 1000:.1f} s en moyenne)")
            if snap["rate_limited"]:
                warnings.append(f"{snap['rate_limited']} dépassement(s) de la limite de fréquence")
        for title in news or []:
            blocking.append(f"incident officiel en cours : {title}")
        for problem in portfolio_problems(portfolio):
            self.emit("anomaly.report", "security_monitor", corr,
                      f"Solde incohérent sur {exchange_id} : {problem}.",
                      {"exchange": exchange_id, "problem": problem}, priority="critical")
            blocking.append(problem)
        status = "down" if blocking else "unknown" if snap is None else "degraded" if warnings else "ok"
        latency = f", {snap['latency_ms']:.0f} ms en moyenne" if snap and snap["latency_ms"] else ""
        text = {"ok": f"{exchange_id} répond normalement{latency}.",
                "unknown": f"{exchange_id} : pas de mesure disponible.",
                "degraded": f"{exchange_id} fonctionne, avec réserves : {' ; '.join(warnings)}.",
                "down": f"{exchange_id} suspendue pour ce cycle : {' ; '.join(blocking)}. "
                        "Les autres plateformes continuent."}[status]
        self.emit("exchange.status", "coordinator", corr, text,
                  {"exchange": exchange_id, "status": status, "tradable": not blocking,
                   "problems": blocking + warnings, "health": snap},
                  status="rejected" if blocking else "ok",
                  priority="high" if blocking else "low", started=started)
        return not blocking


def portfolio_problems(portfolio) -> list[str]:
    """Contrôles de cohérence du portefeuille simulé."""
    problems = []
    if portfolio.cash < -0.01:
        problems.append(f"cash négatif ({eur(portfolio.cash)})")
    if any(p["quantity"] <= 0 for p in portfolio.positions.values()):
        problems.append("position de quantité nulle ou négative")
    value = portfolio.cash + sum(p["quantity"] * p["mark_price"] for p in portfolio.positions.values())
    if abs(value - portfolio.equity) > 0.01 * max(1.0, abs(portfolio.equity)) / 100:
        problems.append(f"capital affiché {eur(portfolio.equity)} différent de cash + positions {eur(value)}")
    return problems


def correlation(a: list | None, b: list | None, window: int = 100) -> float | None:
    """Corrélation des rendements de deux séries [(horodatage, clôture)], alignées sur
    les mêmes bougies. None si moins de 30 bougies communes."""
    if not a or not b:
        return None
    other = dict(b)
    common = [(ts, close, other[ts]) for ts, close in a if ts in other][-(window + 1):]
    if len(common) < 31:
        return None
    ra = [math.log(y[1] / x[1]) for x, y in zip(common, common[1:])]
    rb = [math.log(y[2] / x[2]) for x, y in zip(common, common[1:])]
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else None


class PortfolioOptimizer(Agent):
    name = "portfolio_optimizer"
    role = "Évalue la place d'une proposition dans le portefeuille : corrélations, concentration, cash."
    inputs = ("trade.proposal", "portefeuille", "historique des paires suivies")
    outputs = ("allocation.assessment",)
    depends = ("trade_proposer", "portfolio_manager", "market_intelligence")

    def __init__(self, bus: EventBus, settings: Settings):
        super().__init__(bus)
        self.s = settings

    def assess(self, signal: Signal, portfolio, closes: dict, corr) -> dict:
        started = perf_counter()
        rows, correlated_risk = [], 0.0
        # Positions ouvertes ET ordres en attente : un ordre limite posé est un risque à venir.
        exposures = [(s, p["quantity"] * max(0.0, p["mark_price"] - p["stop_price"]), "position")
                     for s, p in portfolio.positions.items()]
        exposures += [(o["symbol"], o["quantity"] * max(0.0, o["limit_price"] - o["stop_price"]), "ordre en attente")
                      for o in (getattr(portfolio, "pending", None) or {}).values()]
        for symbol, open_risk, kind in exposures:
            rho = 1.0 if symbol == signal.symbol else correlation(
                closes.get(signal.symbol), closes.get(symbol), self.s.correlation_window)
            # Corrélation inconnue = traitée comme totale (fail-closed).
            counted = rho is None or rho >= self.s.correlation_threshold
            if counted:
                correlated_risk += open_risk
            rows.append({"symbol": symbol, "kind": kind, "correlation": rho, "open_risk": open_risk,
                         "counted": counted})
        equity = portfolio.equity
        exposure = sum(p["quantity"] * p["mark_price"] for p in portfolio.positions.values())
        limit = equity * self.s.max_correlated_risk_pct
        linked = [r for r in rows if r["counted"]]
        if not rows:
            text = f"Allocation {signal.symbol} : aucune position ouverte, aucun risque corrélé."
        else:
            parts = [f"{r['symbol']} " + ("corrélation inconnue, comptée comme totale" if r["correlation"] is None
                                          else f"corrélation {r['correlation']:.2f}") for r in rows]
            text = (f"Allocation {signal.symbol} : {len(linked)} position(s) liée(s) sur {len(rows)} "
                    f"({'; '.join(parts)}), risque lié {eur(correlated_risk)} pour {eur(limit)} permis.")
        text += f" Exposition actuelle {exposure / equity:.0%} du capital, cash {eur(portfolio.cash)}." if equity > 0 else ""
        self.emit("allocation.assessment", "risk_manager", corr, text,
                  {"symbol": signal.symbol, "correlations": rows, "correlated_open_risk": correlated_risk,
                   "max_correlated_risk": limit, "exposure": exposure, "cash": portfolio.cash},
                  started=started)
        return {"correlated_open_risk": correlated_risk, "correlations": rows}


class PerformanceAnalyst(Agent):
    name = "performance_analyst"
    role = "Mesure les résultats nets (frais et glissement compris) et met en pause une stratégie qui se dégrade."
    inputs = ("ordres exécutés", "capital de chaque plateforme", "évaluations des stratégies")
    outputs = ("performance.report", "performance.degradation")
    depends = ("execution", "portfolio_manager")

    def __init__(self, bus: EventBus, settings: Settings):
        super().__init__(bus)
        self.s = settings
        self.dir = Path(settings.performance_dir)
        self.pauses = PauseList(settings.pause_path)
        self.last: dict | None = None

    def _curve(self, exchange: str, equity: float | None) -> list:
        path = self.dir / f"{exchange}_equity.jsonl"
        if equity is not None:
            self.dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": utc_now().isoformat(), "equity": equity}) + "\n")
        return [(p["ts"], p["equity"]) for p in load_fills(path)]

    def _backtest(self) -> dict:
        out = {}
        for name in STRATEGIES:
            try:
                ev = json.loads((Path(self.s.evaluations_dir) / f"{name}.json").read_text(encoding="utf-8"))
                out[name] = ev.get("expectancy")
            except (OSError, ValueError):
                out[name] = None
        return out

    def report(self, venues: list[dict], tf_seconds: int, corr: str) -> dict:
        """`venues` : [{"id", "broker", "start"}]. Appelé une fois par tour de cycles."""
        started = perf_counter()
        data = [{"exchange": v["id"], "fills": load_fills(v["broker"].path),
                 "positions": v["broker"].portfolio.positions, "equity": v["broker"].portfolio.equity,
                 "start": v["start"], "curve": self._curve(v["id"], v["broker"].portfolio.equity)}
                for v in venues]
        rep = analyse(data, 365 * 86_400 / tf_seconds, self._backtest())
        rep["shadow"] = {}
        for v in venues:
            coordinator = v.get("coordinator")
            if coordinator is None:
                continue
            book = coordinator.shadow(v["id"])
            reference = book.mark_reference(v.get("watchlist") or [], coordinator.closes,
                                            v["broker"].portfolio.equity)
            rep["shadow"][v["id"]] = {**book.summary(), "reference": reference}
        parts = []
        for row in rep["exchanges"]:
            t = row["trades"]
            exp = "" if t["expectancy"] is None else f", {t['expectancy']:+.2f} € par trade"
            parts.append(f"{row['exchange']} {eur(row['equity'])} ({row['net_return']:+.2%}), "
                         f"{t['trades']} trade(s){exp}, frais {eur(row['fees'])}")
        shadow_parts = []
        for ex_id, sh in rep["shadow"].items():
            ref = sh["reference"]
            if ref:
                shadow_parts.append(f"référence passive {ex_id} {ref['return']:+.2%}")
            for blocker, g in sh["by_blocker"].items():
                if g["resolved"]:
                    shadow_parts.append(f"signaux écartés par {blocker} sur {ex_id} : {g['total_r']:+.1f} R "
                                        f"sur {g['resolved']} résolu(s)")
        self.emit("performance.report", "coordinator", corr,
                  "Performance nette : " + " ; ".join(parts) + "."
                  + (" Shadow : " + " ; ".join(shadow_parts) + "." if shadow_parts else ""),
                  rep, priority="low", started=started)
        for name, st in rep["strategies"].items():
            if st["degradation"] and name in STRATEGIES and not self.pauses.is_paused(name):
                reason = " ; ".join(st["degradation"])
                self.pauses.pause(name, reason, by=self.name)
                self.emit("performance.degradation", "coordinator", corr,
                          f"Stratégie {STRATEGIES[name].label} mise en pause : {reason}. Elle n'ouvre plus "
                          "de position ; une personne doit la relancer.",
                          {"strategy": name, "reasons": st["degradation"]},
                          status="rejected", priority="high")
        self.last = rep
        return rep


class ExecutionIntelligence(Agent):
    name = "execution_intelligence"
    role = "Choisit la manière d'exécuter : ordre au marché ou ordre limite, selon le carnet et les frais."
    inputs = ("risk.decision", "market.order_book", "frais de la plateforme")
    outputs = ("routing.plan",)
    depends = ("risk_manager", "market_intelligence")

    def __init__(self, bus: EventBus, settings: Settings):
        super().__init__(bus)
        self.max_slip = settings.paper_max_slippage_bps / 10_000
        self.min_saving = settings.limit_min_saving_bps

    def plan(self, order, book: dict, taker: float, maker: float, corr) -> dict:
        """Compare l'achat immédiat (au marché) et l'ordre limite posé au meilleur prix
        acheteur. Règle : limite si le carnet ne suffit pas, ou si elle économise au moins
        `limit_min_saving_bps` ; sinon marché (exécution certaine)."""
        started = perf_counter()
        bids, asks = book["bids"], book["asks"]
        mid = (bids[0][0] + asks[0][0]) / 2
        qty, avg = walk_book(asks, order.quantity, order.reference_price * (1 + self.max_slip), True)
        ratio = qty / order.quantity if order.quantity else 0.0
        market_bps = ((avg / mid - 1) * 10_000 if qty else None)
        market_cost = None if market_bps is None else market_bps + taker * 10_000
        limit_price = float(bids[0][0])
        limit_cost = (limit_price / mid - 1) * 10_000 + maker * 10_000
        saving = None if market_cost is None else market_cost - limit_cost
        if ratio < 1 - 1e-9:
            choice, why = "limit", f"le carnet ne couvre que {ratio:.0%} de l'ordre au marché"
        elif saving is not None and saving >= self.min_saving:
            choice, why = "limit", f"l'ordre limite économiserait {saving:.1f} points de base"
        else:
            choice, why = "market", ("exécution immédiate et certaine ; l'ordre limite n'économiserait que "
                                     f"{saving:.1f} points de base et risque de ne pas être exécuté")
        options = {
            "market": {"fill_ratio": ratio, "avg_price": avg or None, "impact_bps": market_bps,
                       "fee_bps": taker * 10_000, "total_cost_bps": market_cost, "certain": True},
            "limit": {"price": limit_price, "fee_bps": maker * 10_000, "total_cost_bps": limit_cost,
                      "certain": False},
        }
        self.emit("routing.plan", "execution", corr,
                  f"Exécution {order.symbol} : {'ordre au marché' if choice == 'market' else 'ordre limite'}"
                  f", car {why}. Coût estimé : marché "
                  + ("impossible" if market_cost is None else f"{market_cost:.1f}")
                  + f" pb, limite {limit_cost:.1f} pb.",
                  {"symbol": order.symbol, "choice": choice, "reason": why, "options": options},
                  started=started)
        return {"choice": choice, "limit_price": limit_price, "options": options}


class ArbitrageAnalyst(Agent):
    """Mesure les écarts de prix entre plateformes. N'exécute rien et ne transfère
    jamais de cryptomonnaie : il affiche et simule seulement."""
    name = "arbitrage_analyst"
    role = "Mesure les écarts de prix entre plateformes, après frais et glissement. Ne transfère jamais rien."
    inputs = ("carnets d'ordres des deux plateformes", "portefeuilles", "frais")
    outputs = ("arbitrage.scan",)
    depends = ("market_intelligence", "exchange_monitor")

    def __init__(self, bus: EventBus, settings: Settings):
        super().__init__(bus)
        self.notional = settings.arbitrage_notional_eur
        self.log = Path(settings.arbitrage_log_path)

    @staticmethod
    def measure(symbol: str, buy: dict, sell: dict, notional: float) -> dict:
        """Acheter sur `buy`, vendre sur `sell` un montant `notional` en EUR."""
        asks, bids = buy["book"]["asks"], sell["book"]["bids"]
        qty = notional / float(asks[0][0])
        q_buy, p_buy = walk_book(asks, qty, float("inf"), True)
        q_sell, p_sell = walk_book(bids, qty, 0.0, False)
        filled = min(q_buy, q_sell)
        gross = (p_sell / p_buy - 1) * 10_000 if filled else None
        fees = (buy["taker"] + sell["taker"]) * 10_000
        top_gap = (float(bids[0][0]) / float(asks[0][0]) - 1) * 10_000
        net = None if gross is None else gross - fees
        inventory = sell["positions"].get(symbol, {}).get("quantity", 0.0)
        reasons = []
        if net is None or net <= 0:
            reasons.append("écart inférieur aux frais et au glissement")
        if inventory < qty:
            reasons.append(f"aucun {symbol.split('/')[0]} détenu sur {sell['id']} pour vendre "
                           "(il faudrait un transfert, exclu)")
        if buy["cash"] < notional:
            reasons.append(f"cash insuffisant sur {buy['id']}")
        return {"symbol": symbol, "buy_on": buy["id"], "sell_on": sell["id"], "notional": notional,
                "top_gap_bps": top_gap, "gross_bps": gross, "fees_bps": fees, "net_bps": net,
                "fill_ratio": filled / qty if qty else 0.0,
                "latency_ms": (buy.get("latency_ms") or 0) + (sell.get("latency_ms") or 0),
                "exploitable": not reasons, "reasons": reasons,
                "hypothetical_pnl": None if net is None else notional * net / 10_000}

    async def scan(self, venues: list[dict], corr: str) -> list[dict]:
        if len(venues) < 2:
            return []
        started = perf_counter()
        common = set(venues[0]["watchlist"])
        for v in venues[1:]:
            common &= set(v["watchlist"])
        sides = []
        for v in venues:
            health = getattr(v["ex"], "health", None)
            sides.append({"id": v["id"], "ex": v["ex"], "positions": v["broker"].portfolio.positions,
                          "cash": v["broker"].portfolio.cash - v["broker"].reserved(),
                          "taker": v["coordinator"].settings.paper_fee_rate,
                          "latency_ms": health.snapshot()["latency_ms"] if health else None})
        rows = []
        for symbol in sorted(common):
            books = {}
            for side in sides:
                books[side["id"]] = await side["ex"].order_book(symbol, limit=50)
            best = None
            for a in sides:
                for b in sides:
                    if a is b:
                        continue
                    row = self.measure(symbol, {**a, "book": books[a["id"]]},
                                       {**b, "book": books[b["id"]]}, self.notional)
                    if best is None or (row["net_bps"] or -1e9) > (best["net_bps"] or -1e9):
                        best = row
            rows.append(best)
        if rows:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open("a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps({"ts": utc_now().isoformat(), **row}) + "\n")
        parts = [f"{r['symbol']} : acheter sur {r['buy_on']}, vendre sur {r['sell_on']}, écart brut "
                 f"{r['gross_bps']:+.1f} pb, net {r['net_bps']:+.1f} pb après frais"
                 + (" — exploitable en simulation" if r["exploitable"] else f" — non exploitable ({r['reasons'][0]})")
                 for r in rows if r["gross_bps"] is not None]
        self.emit("arbitrage.scan", "coordinator", corr,
                  ("Arbitrage (théorique, aucun transfert) : " + " ; ".join(parts) + ".") if parts
                  else "Arbitrage : aucune paire suivie en commun sur les plateformes.",
                  {"pairs": rows}, priority="low", started=started)
        return rows


class AIAnalyst(Agent):
    """Grok, par l'API officielle de xAI. Donne des avis marqués « non vérifiés » ; ne
    peut émettre que des messages `ai.*`, donc ni ordre, ni risque, ni configuration."""
    name = "ai_analyst"
    role = "IA d'avis (ChatGPT ou Grok) : interprète les actualités et explique les décisions. Avis seulement."
    inputs = ("news.update", "fils de décision (à la demande)")
    outputs = ("ai.interpretation", "ai.explanation")
    depends = ("news_analyst",)
    MAX_NEW_PER_ROUND = 3

    def __init__(self, bus: EventBus, settings: Settings):
        super().__init__(bus)
        from trading_agent.ai import make_client
        self.client = make_client(settings)
        self.cache_path = Path(settings.ai_cache_path)
        try:
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}

    def _remember(self, key: str, value: dict) -> None:
        self.cache[key] = value
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    async def interpret_news(self, news_result: dict, corr: str) -> int:
        """Ajoute l'avis de Grok aux avis officiels qui concernent le trading ou un actif
        suivi. L'information vérifiée reste intacte, l'avis est rangé à part."""
        from trading_agent.ai import AIUnavailable, news_prompt
        new = 0
        for result in news_result.values():
            for item in result.get("items") or []:
                if item.get("relevance") == "autre":
                    continue
                key = f"news|{item['exchange']}|{item['title']}|{item.get('status')}"
                if key not in self.cache:
                    if not self.client.enabled or new >= self.MAX_NEW_PER_ROUND:
                        continue
                    started = perf_counter()
                    try:
                        text = await asyncio.to_thread(self.client.ask, news_prompt(item))
                    except AIUnavailable:
                        return new
                    self._remember(key, {"text": text, "model": self.client.model, "verified": False,
                                         "at": utc_now().isoformat()})
                    new += 1
                    self.emit("ai.interpretation", "coordinator", corr,
                              f"Avis de {self.client.label} (non vérifié) sur « {item['title']} » ({item['exchange']}) : {text}",
                              {"exchange": item["exchange"], "title": item["title"], "text": text,
                               "model": self.client.model, "verified": False}, priority="low", started=started)
                item["interpretation"] = self.cache[key]
        return new

    async def explain(self, corr: str, events: list[dict]) -> dict:
        from trading_agent.ai import thread_prompt
        key = f"fil|{corr}"
        if key in self.cache:
            return {**self.cache[key], "cached": True}
        started = perf_counter()
        text = await asyncio.to_thread(self.client.ask, thread_prompt(events), 450)
        value = {"text": text, "model": self.client.model, "verified": False, "at": utc_now().isoformat()}
        self._remember(key, value)
        self.emit("ai.explanation", "coordinator", corr, f"Explication de {self.client.label} (non vérifiée) : {text}",
                  {"text": text, "model": self.client.model, "verified": False}, priority="low", started=started)
        return {**value, "cached": False}


class NewsAnalyst(Agent):
    name = "news_analyst"
    role = "Lit les incidents et maintenances publiés par les plateformes. Sépare le vérifié de l'interprété."
    inputs = ("pages d'état officielles des plateformes",)
    outputs = ("news.update", "news.calendar")
    depends = ()

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        self.latest: dict[str, dict] = {}
        self.blocking: dict[str, list[str]] = {}   # plateforme -> incidents bloquants en cours
        self.calendar: dict | None = None          # calendrier économique, affiché seulement

    async def check(self, watchlists: dict[str, list[str]], corr: str, fetcher=None) -> dict:
        from trading_agent import news
        started = perf_counter()
        result = await news.collect(watchlists, fetcher or news.fetch)
        self.latest = result
        self.blocking = {ex: r["blocking"] for ex, r in result.items() if r.get("blocking")}
        parts = []
        for ex, r in result.items():
            if r.get("error"):
                parts.append(f"{ex} : page d'état injoignable ({r['error']})")
                continue
            relevant = [i for i in r["items"] if i["relevance"] != "autre"]
            parts.append(f"{ex} : {r['description'] or 'état inconnu'}, {len(r['items'])} avis officiel(s)"
                         + (f", dont {len(relevant)} concernant le trading ou un actif suivi" if relevant else "")
                         + (f" — BLOQUANT : {'; '.join(r['blocking'])}" if r.get("blocking") else ""))
        self.emit("news.update", "exchange_monitor", corr,
                  "Actualités vérifiées (pages d'état officielles) : " + " ; ".join(parts)
                  + ". Aucune interprétation par une IA.", {"exchanges": result},
                  priority="high" if self.blocking else "low", started=started)
        return result

    def check_calendar(self, corr: str, path: str | None = None, now=None) -> dict:
        """Calendrier économique : information seulement. Aucune décision ne lit `self.calendar`."""
        from trading_agent import macro_calendar
        started = perf_counter()
        calendar = macro_calendar.load(path, now)
        calendar["upcoming"] = macro_calendar.upcoming(calendar, now) if not calendar["error"] else []
        self.calendar = calendar
        self.emit("news.calendar", "dashboard", corr, macro_calendar.summary(calendar, now),
                  {"upcoming": calendar["upcoming"], "error": calendar["error"], "stale": calendar["stale"],
                   "covered_until": calendar["covered_until"]},
                  status="error" if calendar["error"] else "ok",
                  priority="normal" if calendar["error"] or calendar["stale"] else "low", started=started)
        return calendar


class LearningManager(Agent):
    name = "learning_manager"
    role = "Analyse les décisions passées et propose des réglages testés à part. N'applique rien lui-même."
    inputs = ("rapports de performance", "mode Shadow", "refus enregistrés", "historique des marchés")
    outputs = ("learning.review", "learning.proposal")
    depends = ("performance_analyst", "risk_manager")

    def __init__(self, bus: EventBus, settings: Settings):
        super().__init__(bus)
        self.store = ParamStore(settings.learning_dir)
        self.last_review_day: str | None = None
        self.busy = False

    def review(self, report: dict | None, pauses: dict, corr: str, force: bool = False) -> dict | None:
        """Bilan quotidien des décisions : ce qui a été refusé, par qui, et si les
        protections ont aidé (mode Shadow). Ne modifie rien."""
        day = utc_now().date().isoformat()
        if not force and day == self.last_review_day:
            return None
        self.last_review_day = day
        started = perf_counter()
        from collections import Counter
        refusals: Counter = Counter()
        errors: Counter = Counter()
        if self.bus.store:
            for e in self.bus.store.search(type_prefix="risk.decision", status="rejected", limit=500):
                refusals[RISK_REASONS_FR.get(e["payload"].get("reason"), e["payload"].get("reason"))] += 1
            for e in self.bus.store.search(type_prefix="agent.error", limit=500):
                errors[e["payload"].get("agent", "?")] += 1
        findings, suggestions = [], []
        for name, p in pauses.items():
            findings.append(f"{name} en pause ({p['reason']})")
            suggestions.append(f"proposer un nouveau réglage pour {name}, ou la laisser en pause")
        for ex_id, sh in ((report or {}).get("shadow") or {}).items():
            for blocker, g in sh["by_blocker"].items():
                if g["resolved"] >= 10:
                    verdict = "a évité des pertes" if g["total_r"] < 0 else "a coûté des gains"
                    findings.append(f"le filtre « {blocker} » sur {ex_id} {verdict} ({g['total_r']:+.1f} R "
                                    f"sur {g['resolved']} signaux)")
                    if g["total_r"] > 0:
                        suggestions.append(f"réexaminer le filtre « {blocker} » : il écarte des signaux gagnants")
        if errors:
            findings.append("erreurs par agent : " + ", ".join(f"{a} {n}" for a, n in errors.most_common(3)))
        text = ("Bilan d'apprentissage : " + ("; ".join(findings) if findings else "rien à signaler")
                + (". Pistes : " + " ; ".join(suggestions) if suggestions else "")
                + ". Aucune modification n'est faite sans décision humaine.")
        payload = {"refusals": dict(refusals.most_common(10)), "errors": dict(errors),
                   "findings": findings, "suggestions": suggestions}
        self.emit("learning.review", "coordinator", corr, text, payload, priority="low", started=started)
        return payload

    def publish_proposal(self, proposal: dict, corr: str) -> None:
        self.store.save(proposal)
        best = proposal["best"]
        change = ", ".join(f"{k} = {v}" for k, v in (best or {}).get("params", {}).items()) or "aucun"
        test = (best or {}).get("test") or {}
        text = (f"Proposition de réglage {proposal['id']} pour {proposal['strategy']} : {change} "
                f"({proposal['tried']} réglages essayés). "
                + (f"Sur la période de contrôle : {test.get('expectancy') or 0:+.2f} € par trade sur "
                   f"{test.get('trades', 0)} trades. " if test else "")
                + ("Retenue : une personne peut l'appliquer." if proposal["accepted"]
                   else "Non retenue : " + " ; ".join(proposal["reasons"]) + "."))
        self.emit("learning.proposal", "coordinator", corr, text,
                  {k: proposal[k] for k in ("id", "strategy", "tried", "accepted", "reasons", "status")},
                  status="ok" if proposal["accepted"] else "rejected")


class _Suspended(Exception):
    """Plateforme indisponible pour ce cycle : on n'opère pas, sans arrêter le reste."""


class AdversarialAnalyst(Agent):
    name = "adversarial_analyst"
    role = "Cherche les raisons pour lesquelles l'opération pourrait échouer, avant la proposition."
    inputs = ("strategy.signal", "market.order_book", "regime.detected")
    outputs = ("adversarial.review",)
    depends = ("strategy_analyst", "market_intelligence", "regime_detector")

    def __init__(self, bus: EventBus, settings: Settings):
        super().__init__(bus)
        self.fee = settings.paper_fee_rate
        self.slip = settings.paper_slippage_bps / 10_000
        self.min_depth = settings.adversarial_min_depth_eur
        self.reward_risk = settings.reward_risk

    def review(self, signal: Signal, snap: MarketSnapshot, book: dict, corr) -> dict:
        started = perf_counter()
        objections = adversarial_objections(
            signal, snap.candles, snap.regime, int(snap.max_age_s * 1000) if snap.max_age_s else None,
            self.fee, self.slip, self.reward_risk, book=book, min_depth=self.min_depth)
        majors = [o for o in objections if o["severity"] == "majeure"]
        verdict = "reject" if majors else "pass"
        if majors:
            text = "Objection majeure : " + " ; ".join(o["text"] for o in majors) + "."
        elif objections:
            text = (f"Aucune objection majeure, {len(objections)} point(s) d'attention : "
                    + " ; ".join(o["text"] for o in objections) + ".")
        else:
            text = "Aucune objection : rien ne s'oppose à l'opération."
        self.emit("adversarial.review", "trade_proposer" if verdict == "pass" else "coordinator", corr,
                  text, {"symbol": signal.symbol, "strategy": signal.strategy, "verdict": verdict,
                         "objections": objections},
                  status="ok" if verdict == "pass" else "rejected", started=started)
        return {"verdict": verdict, "objections": objections}


def expired(proposal: dict | None) -> bool:
    """Une proposition sans date limite lisible est considérée comme expirée."""
    try:
        return datetime.now(timezone.utc) > datetime.fromisoformat(proposal["valid_until"])
    except (TypeError, KeyError, ValueError):
        return True


class TradeProposer(Agent):
    """Trade Manager : transforme un signal retenu en proposition identifiée, chiffrée
    et datée. Une proposition expirée ne peut plus être exécutée."""
    name = "trade_proposer"
    role = "Trade Manager : transforme un signal retenu en proposition identifiée, chiffrée et datée."
    inputs = ("strategy.signal", "adversarial.review")
    outputs = ("trade.proposal",)
    depends = ("strategy_analyst", "adversarial_analyst")

    def __init__(self, bus: EventBus, settings: Settings | None = None):
        super().__init__(bus)
        settings = settings or Settings(_env_file=None)
        self.reward_risk = settings.reward_risk
        self.ttl = settings.proposal_ttl_s
        self.fee = settings.paper_fee_rate
        self.slip = settings.paper_slippage_bps / 10_000
        self.evaluations_dir = Path(settings.evaluations_dir)

    def _track_record(self, strategy: str) -> dict | None:
        """Historique de la stratégie : sa dernière évaluation, s'il y en a une."""
        path = self.evaluations_dir / f"{strategy}.json"
        try:
            ev = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return {"trades": ev.get("trades"), "expectancy": ev.get("expectancy"),
                "profit_factor": ev.get("profit_factor"), "evaluated_at": ev.get("evaluated_at"),
                "passed": ev.get("passed")}

    def propose(self, signal: Signal, corr, timeframe: str = "",
                objections: list | None = None) -> dict:
        started = perf_counter()
        entry, stop = signal.reference_price, signal.stop_price
        risk = entry - stop
        created = datetime.now(timezone.utc)
        cost = entry * 2 * (self.fee + self.slip)
        track = self._track_record(signal.strategy)
        proposal = {
            "proposal_id": uuid.uuid4().hex[:12],
            "created_at": created.isoformat(),
            "valid_until": (created + timedelta(seconds=self.ttl)).isoformat(),
            "symbol": signal.symbol, "side": signal.side.value, "strategy": signal.strategy,
            "horizon": f"{STRATEGIES[signal.strategy].horizon} (bougies {timeframe})"
                       if signal.strategy in STRATEGIES else timeframe,
            "entry_price": entry, "stop_price": stop,
            "take_profit": entry + self.reward_risk * risk,
            "reward_risk": self.reward_risk, "confidence": signal.confidence,
            "estimated_costs_per_unit": cost,
            "estimated_fees_per_unit": entry * 2 * self.fee,
            "estimated_slippage_per_unit": entry * 2 * self.slip,
            "gain_if_target_per_unit": self.reward_risk * risk - cost,
            "loss_if_stop_per_unit": risk + cost,
            "expectancy_estimate": track["expectancy"] if track else None,
            "track_record": track,
            "rationale": signal.rationale,
            "cancel_if": [STRATEGIES[signal.strategy].cancel_if if signal.strategy in STRATEGIES
                          else "le prix passe sous le stop avant l'exécution",
                          f"la proposition n'est pas exécutée avant {self.ttl} s"],
            "objections": objections or [],
            "sizing": "montant fixé par le Risk Manager",
        }
        hist = ("pas encore d'historique évalué" if not track or track["expectancy"] is None
                else f"historique : {track['expectancy']:+.2f} € par trade sur {track['trades']} trades")
        event = self.emit(
            "trade.proposal", "risk_manager", corr,
            f"Proposition {proposal['proposal_id']} : acheter {signal.symbol} à {eur(entry)}, "
            f"stop {eur(stop)}, objectif {eur(proposal['take_profit'])} (ratio {self.reward_risk:g}), "
            f"valable {self.ttl} s, {hist}.", proposal, started=started)
        return {**proposal, "event_id": event.id}


class RiskAgent(Agent):
    name = "risk_manager"
    role = "Contrôle chaque proposition avec des règles fixes. En cas de doute, refuse."
    inputs = ("trade.proposal", "portefeuille", "état du marché")
    outputs = ("risk.decision",)
    depends = ("trade_proposer", "portfolio_manager")

    def __init__(self, bus: EventBus, settings: Settings):
        super().__init__(bus)
        self.engine = RiskManager(settings)

    def review(self, signal: Signal, portfolio, corr, market: dict,
               proposal: dict | None = None) -> RiskDecision:
        started = perf_counter()
        proposal = proposal or {}
        pid = proposal.get("proposal_id")
        try:
            if self.bus.halted:
                decision = RiskDecision(False, "système à l'arrêt")
            elif not pid:
                decision = RiskDecision(False, "proposition sans identifiant")
            elif expired(proposal):
                decision = RiskDecision(False, "proposition expirée")
            else:
                decision = self.engine.evaluate(signal, portfolio, market)
        except Exception as exc:  # fail-closed : toute erreur vaut refus
            decision = RiskDecision(False, f"erreur du moteur de risque ({exc})")
        if decision.approved:
            order = decision.order
            self.emit("risk.decision", "execution", corr,
                      f"Accepté : achat de {order.quantity:.8f} {order.symbol.split('/')[0]} "
                      f"(environ {eur(order.quantity * order.reference_price)}).",
                      {"approved": True, "proposal_id": pid, "quantity": order.quantity,
                       "notional": order.quantity * order.reference_price,
                       "stop_price": order.stop_price}, started=started,
                      reply_to=proposal.get("event_id"))
        else:
            reason = RISK_REASONS_FR.get(decision.reason, decision.reason)
            self.emit("risk.decision", "coordinator", corr, f"Refusé : {reason}.",
                      {"approved": False, "proposal_id": pid, "reason": decision.reason},
                      status="rejected", started=started, reply_to=proposal.get("event_id"))
        return decision


class ExecutionAgent(Agent):
    name = "execution"
    role = "Simule les ordres. N'achète jamais sans validation du Risk Manager."
    inputs = ("risk.decision", "market.order_book", "strategy.exit_signal")
    outputs = ("execution.fill", "execution.rejected")
    depends = ("risk_manager", "market_intelligence")

    def __init__(self, bus: EventBus, broker: PaperBroker):
        super().__init__(bus)
        self.broker = broker
        bus.subscribe(self._on_event)

    def _on_event(self, event: Event) -> None:
        # Arrêt d'urgence : tout ordre en attente est annulé sur-le-champ.
        if event.type == "system.halt":
            for order_id in list(self.broker.portfolio.pending):
                cancelled = self.broker.cancel(order_id, "emergency_halt")
                self.emit("execution.cancelled", "portfolio_manager", event.correlation_id,
                          f"Ordre limite {order_id} sur {cancelled['symbol']} annulé : arrêt d'urgence.",
                          cancelled, status="rejected", priority="high")

    def _guard(self, proposal: dict | None) -> str | None:
        pid = (proposal or {}).get("proposal_id")
        if self.bus.halted:
            return "système à l'arrêt."
        if not self.bus.recording_ok:
            return "base de données indisponible : aucun achat sans enregistrement complet."
        if not self.bus.is_approved(pid):
            return ("proposition déjà exécutée." if pid in self.bus._executed
                    else "aucune validation du Risk Manager pour cette proposition.")
        if expired(proposal):
            return "proposition expirée : elle ne peut plus être exécutée."
        return None

    def place_limit(self, order, limit_price: float, placed_ms: int, expires_ms: int,
                    maker_fee: float, corr, proposal: dict | None = None,
                    max_participation: float = 0.10) -> dict:
        started = perf_counter()
        pid = (proposal or {}).get("proposal_id")
        reason = self._guard(proposal)
        if reason:
            return self._report({"status": "rejected", "reason": reason, "proposal_id": pid},
                                corr, started, "")
        result = self.broker.place_limit(order, limit_price, placed_ms, expires_ms, maker_fee, pid,
                                         max_participation)
        if result["status"] != "placed":
            return self._report({**result, "proposal_id": pid}, corr, started, "")
        self.emit("execution.order_placed", "portfolio_manager", corr,
                  f"Ordre limite {result['order_id']} posé : achat de {result['quantity']:.8f} "
                  f"{order.symbol} à {eur(limit_price)} au plus, valable jusqu'à la clôture de la "
                  f"prochaine bougie. {eur(result['reserved'])} réservés.", result, started=started)
        return result

    def check_pending(self, symbol: str, candles: list, corr) -> list[dict]:
        started = perf_counter()
        results = self.broker.check_pending(symbol, candles)
        for r in results:
            if r["status"] == "filled":
                self._report(r, corr, started, "Ordre limite exécuté, achat simulé")
            elif r.get("filled_quantity", 0) > 0:
                why = ("la partie achetée a été vendue par le stop, on ne rachète pas"
                       if r.get("reason") == "position_closed" else "l'ordre a expiré")
                self.emit("execution.expired", "portfolio_manager", corr,
                          f"Reste de l'ordre limite {r['order_id']} sur {symbol} annulé ({why}) : "
                          f"{r['filled_quantity']:.8f} achetés sur {r['requested_quantity']:.8f} demandés. "
                          "Montant réservé restant libéré.", r, status="rejected", started=started)
            else:
                self.emit("execution.expired", "portfolio_manager", corr,
                          f"Ordre limite {r['order_id']} sur {symbol} expiré sans exécution : "
                          "le prix n'est pas descendu jusqu'à la limite. Montant réservé libéré.",
                          r, status="rejected", started=started)
        return results

    def _report(self, result: dict, corr, started, label: str) -> dict:
        if result.get("status") == "filled":
            text = (f"{label} : {result['quantity']:.8f} {result['symbol']} à "
                    f"{eur(result['fill_price'])}, frais {eur(result['fee'])}")
            if result.get("partial") and result.get("order_type") == "limit":
                text += (f" (exécution partielle : {result['filled_quantity']:.8f} sur "
                         f"{result['requested_quantity']:.8f}, peu de volume échangé sous la limite"
                         + (", le reste attend" if result.get("remaining_quantity") else "") + ")")
            elif result.get("partial"):
                text += " (exécution partielle : carnet insuffisant)"
            if "realized_pnl" in result:
                text += f", résultat {eur(result['realized_pnl'])}"
            self.emit("execution.fill", "portfolio_manager", corr, text + ".", result, started=started)
        else:
            self.emit("execution.rejected", "coordinator", corr,
                      f"Ordre simulé rejeté : {result.get('reason', 'raison inconnue')}",
                      result, status="rejected", started=started)
        return result

    def open(self, order, candle_ms: int, corr, asks: list | None = None,
             proposal: dict | None = None) -> dict:
        started = perf_counter()
        pid = (proposal or {}).get("proposal_id")
        reason = self._guard(proposal)
        if reason:
            return self._report({"status": "rejected", "reason": reason, "proposal_id": pid},
                                corr, started, "")
        result = self.broker.buy(order, candle_ms=candle_ms, asks=asks)
        if result.get("status") == "filled" and order.symbol in self.broker.portfolio.positions:
            # La position garde la trace de sa proposition : la vente pourra y être reliée.
            self.broker.portfolio.positions[order.symbol]["proposal_id"] = pid
            self.broker._save()
        return self._report({**result, "proposal_id": pid}, corr, started, "Achat simulé")

    def check_stops(self, symbol: str, candles: list, corr) -> dict | None:
        started = perf_counter()
        result = self.broker.check_stops(symbol, candles)
        return self._report(result, corr, started, "Stop touché, vente simulée") if result else None

    def close(self, symbol: str, price: float, corr, bids: list | None = None) -> dict:
        started = perf_counter()
        return self._report(self.broker.close_position(symbol, price, reason="exit_signal", bids=bids),
                            corr, started, "Vente simulée sur signal de sortie")

    def flatten(self, corr) -> list[dict]:
        """Fermeture d'urgence : vend toutes les positions simulées au dernier prix connu.
        Réservée à une décision humaine (jamais déclenchée par un agent)."""
        results = []
        for symbol, position in list(self.broker.portfolio.positions.items()):
            started = perf_counter()
            results.append(self._report(
                self.broker.close_position(symbol, position["mark_price"], reason="emergency_flatten"),
                corr, started, "Fermeture d'urgence, vente simulée au dernier prix connu"))
        return results


class PortfolioManager(Agent):
    name = "portfolio_manager"
    role = "Valorise le portefeuille : capital, cash, positions, gains et pertes."
    inputs = ("market.snapshot", "execution.fill")
    outputs = ("portfolio.update",)
    depends = ("market_intelligence", "execution")

    def __init__(self, bus: EventBus, broker: PaperBroker):
        super().__init__(bus)
        self.broker = broker

    def update(self, prices: dict, corr) -> None:
        started = perf_counter()
        self.broker.mark(prices)
        p = self.broker.portfolio
        exposure = sum(pos["quantity"] * pos["mark_price"] for pos in p.positions.values())
        self.emit("portfolio.update", "auditor", corr,
                  f"Capital {eur(p.equity)}, cash {eur(p.cash)}, {len(p.positions)} position(s), "
                  f"résultat du jour {eur(p.daily_pnl)}.",
                  {"equity": p.equity, "cash": p.cash, "exposure": exposure,
                   "daily_pnl": p.daily_pnl, "weekly_pnl": p.weekly_pnl,
                   "positions": p.positions}, started=started)


class SecurityMonitor(Agent):
    name = "security_monitor"
    role = "Surveille les erreurs répétées et l'emballement. Peut arrêter le système."
    inputs = ("tous les événements",)
    outputs = ("security.alert", "system.halt")
    depends = ()

    def __init__(self, bus: EventBus, max_error_cycles: int = 5, max_events_per_minute: int = 300):
        super().__init__(bus)
        self.max_error_cycles = max_error_cycles
        self.max_events_per_minute = max_events_per_minute
        self.error_cycles = 0
        self.recent: deque = deque()
        bus.subscribe(self.on_event)

    def on_event(self, event: Event) -> None:
        if event.source == self.name or self.bus.halted:
            return
        now = event.ts.timestamp()
        self.recent.append(now)
        while self.recent and now - self.recent[0] > 60:
            self.recent.popleft()
        if event.type == "cycle.end":
            self.error_cycles = self.error_cycles + 1 if event.status == "error" else 0
        reason = None
        if event.type.startswith("anomaly.") and event.priority == "critical":
            reason = f"anomalie critique signalée par {event.source} : {event.result}"
        elif self.error_cycles >= self.max_error_cycles:
            reason = f"{self.error_cycles} cycles consécutifs en erreur"
        elif len(self.recent) > self.max_events_per_minute:
            reason = f"plus de {self.max_events_per_minute} événements en une minute"
        if reason:
            corr = event.correlation_id
            self.emit("security.alert", "coordinator", corr, f"Anomalie : {reason}.",
                      {"reason": reason}, status="error")
            self.emit("system.halt", "coordinator", corr,
                      f"Arrêt du système demandé par la sécurité : {reason}.")


class Auditor(Agent):
    name = "auditor"
    role = "Vérifie que chaque fil est enregistré et que le journal n'a pas été modifié."
    inputs = ("tous les événements du fil",)
    outputs = ("audit.thread_recorded",)
    depends = ()

    def close_thread(self, corr) -> None:
        events = self.bus.thread(corr)
        self.emit("audit.thread_recorded", "coordinator", corr,
                  f"{len(events)} événements enregistrés pour ce fil.",
                  {"events": len(events), "last_hash": events[-1].hash if events else ""})

    def verify(self) -> tuple[bool, int, int | None]:
        return verify_chain(str(self.bus.path)) if self.bus.path else (True, len(self.bus.events), None)


class Coordinator(Agent):
    name = "coordinator"
    role = "Chef d'orchestre : lance les cycles et fait travailler les agents dans l'ordre."
    inputs = ("horloge des bougies", "demandes de l'interface")
    outputs = ("cycle.start", "cycle.end", "agent.error", "system.halt", "system.resume", "registry.change")
    depends = ("tous les agents",)

    def __init__(self, bus: EventBus, settings: Settings, broker: PaperBroker,
                 security: "SecurityMonitor | None" = None):
        super().__init__(bus)
        self.broker = broker
        self.settings = settings
        self.closes: dict[str, list] = {}   # dernières clôtures de chaque paire suivie
        self.shadows: dict[str, ShadowBook] = {}
        self.monitor = ExchangeMonitor(bus)
        self.optimizer = PortfolioOptimizer(bus, settings)
        self.market = MarketIntelligence(bus)
        self.regime = RegimeDetector(bus)
        self.strategy = StrategyAnalyst(bus, settings.strategy, settings.regime_filter,
                                        settings.pause_path, settings.learning_dir, settings.hmm_filter)
        self.learning = LearningManager(bus, settings)
        self.news = NewsAnalyst(bus)
        self.ai = AIAnalyst(bus, settings)
        self.news_source: "NewsAnalyst | None" = None   # analyste partagé entre plateformes
        self.performance = PerformanceAnalyst(bus, settings)
        self.router = ExecutionIntelligence(bus, settings)
        self.arbitrage = ArbitrageAnalyst(bus, settings)
        self.adversary = AdversarialAnalyst(bus, settings)
        self.proposer = TradeProposer(bus, settings)
        self.risk = RiskAgent(bus, settings)
        self.execution = ExecutionAgent(bus, broker)
        self.portfolio = PortfolioManager(bus, broker)
        # Plusieurs coordinateurs (un par plateforme) partagent le même Security Monitor.
        self.security = security or SecurityMonitor(bus)
        self.auditor = Auditor(bus)
        self.agents = [self, self.monitor, self.market, self.regime, self.strategy,
                       self.adversary, self.proposer, self.optimizer, self.risk, self.execution,
                       self.portfolio, self.performance, self.router, self.arbitrage, self.learning,
                       self.news, self.ai,
                       self.security, self.auditor]
        self._current: Agent = self
        self.lab = None  # laboratoire de stratégies (optionnel, voir lab.py)

    def shadow(self, exchange_id: str) -> ShadowBook:
        if exchange_id not in self.shadows:
            self.shadows[exchange_id] = ShadowBook(
                str(Path(self.settings.shadow_dir) / f"{exchange_id}.json"), self.settings.paper_fee_rate,
                self.settings.paper_slippage_bps, self.settings.reward_risk)
        return self.shadows[exchange_id]

    def _shade(self, exchange_id: str, signal, blocked_by: str, reason: str, candle_ms: int, corr: str):
        """Mode Shadow : on garde la trace du signal écarté. Jamais bloquant."""
        try:
            self.shadow(exchange_id).record(signal, blocked_by, reason, candle_ms, corr)
        except Exception as exc:
            self.last_error = f"shadow : {exc}"

    def _use(self, agent: Agent) -> Agent:
        """Passe la main à un agent ; refuse s'il a été arrêté."""
        self._current = agent
        if not agent.enabled:
            agent.status = "stopped"
            raise RuntimeError("agent arrêté manuellement")
        agent.status = "active"
        return agent

    def _run_lab(self, snap: MarketSnapshot, corr: str) -> None:
        """Compétition entre stratégies sur des portefeuilles fictifs séparés.
        Une panne du laboratoire ne doit jamais perturber le cycle principal."""
        try:
            trades = self.lab.step(snap, {
                "data_age_s": snap.data_age_s, "max_age_s": snap.max_age_s,
                "spread_bps": snap.spread_bps, "min_amount": snap.min_amount,
                "min_cost": snap.min_cost})
            self.lab.last_error = None
        except Exception as exc:
            self.lab.last_error = str(exc)
            return
        for trade in trades:
            action = "achète" if trade["side"] == "buy" else "vend"
            text = (f"Laboratoire : la stratégie {trade['label']} {action} à "
                    f"{eur(trade['fill_price'])} dans son portefeuille fictif séparé")
            if "realized_pnl" in trade:
                text += f", résultat {eur(trade['realized_pnl'])}"
            self.strategy.emit("strategy.lab", "coordinator", corr, text + ".",
                               {k: trade[k] for k in ("strategy", "side", "quantity", "fill_price", "fee")})

    def halt(self, reason: str, corr: str = "manual") -> None:
        self.emit("system.halt", "coordinator", corr, f"Arrêt d'urgence : {reason}.")

    async def watch_stops(self, ex, exchange_id: str, timeframe: str) -> str | None:
        """Pendant un arrêt d'urgence : vérifie seulement les stops des positions ouvertes.
        Aucune analyse, aucun achat ; seules des ventes de protection peuvent partir."""
        symbols = list(self.broker.portfolio.positions)
        if not symbols:
            return None
        corr = uuid.uuid4().hex[:12]
        started = perf_counter()
        self.emit("cycle.start", "execution", corr,
                  f"Arrêt d'urgence en cours : surveillance des stops seulement, sans aucun achat, "
                  f"pour {', '.join(symbols)} sur {exchange_id}.",
                  {"exchange": exchange_id, "symbols": symbols, "mode": "stops_only"}, priority="high")
        closed, problems = [], []
        tf_ms = ex.timeframe_seconds(timeframe) * 1000
        for symbol in symbols:
            try:
                candles = closed_candles(await ex.ohlcv(symbol, timeframe=timeframe, limit=50), tf_ms, ex.now_ms())
                if not candles:
                    raise RuntimeError("aucune bougie close reçue")
                self.broker.mark({symbol: float(candles[-1][4])})
                fill = self.execution.check_stops(symbol, candles, corr)
                if fill and fill.get("status") == "filled":
                    closed.append(symbol)
            except Exception as exc:   # une panne ici ne doit rien déclencher d'autre
                problems.append(f"{symbol} : {exc}")
        outcome = (f"stop touché et position vendue pour {', '.join(closed)}" if closed
                   else "aucun stop touché, positions conservées")
        if problems:
            outcome += " ; vérification impossible pour " + " ; ".join(problems)
        self.emit("cycle.end", "auditor", corr, f"Surveillance des stops terminée : {outcome}.",
                  {"outcome": outcome, "mode": "stops_only"}, status="error" if problems else "ok",
                  started=started)
        self.auditor.close_thread(corr)
        return corr

    async def run_daily_lab(self, lab, ex, exchange_id: str, symbols: list[str]) -> list[dict]:
        """Laboratoire journalier : chaque stratégie joue sur les bougies d'un jour, avec
        son propre portefeuille fictif. Sert à obtenir des preuves futures, jamais vues en
        backtest. Une panne ici ne touche jamais le cycle principal."""
        from types import SimpleNamespace
        tf_ms = 86_400_000
        trades = []
        for symbol in symbols:
            try:
                now_ms = ex.now_ms()
                candles = closed_candles(await ex.ohlcv(symbol, timeframe="1d", limit=300), tf_ms, now_ms)
                if len(candles) < 2:
                    continue
                ticker = await ex.ticker(symbol) or {}
                bid, ask = ticker.get("bid"), ticker.get("ask")
                spread = (ask - bid) / ((ask + bid) / 2) * 10_000 if bid and ask and ask >= bid > 0 else None
                snap = SimpleNamespace(exchange=exchange_id, symbol=symbol, timeframe="1d", candles=candles,
                                       last_price=float(candles[-1][4]), regime=classify(candles))
                limits = ex.limits(symbol) if hasattr(ex, "limits") else {}
                done = lab.step(snap, {"data_age_s": (now_ms - candles[-1][0] - tf_ms) / 1000,
                                       "max_age_s": 86_400, "spread_bps": spread, **limits})
                lab.last_error = None
            except Exception as exc:
                lab.last_error = f"{symbol} : {exc}"
                continue
            for trade in done:
                action = "achète" if trade["side"] == "buy" else "vend"
                text = (f"Laboratoire journalier : la stratégie {trade['label']} {action} {symbol} à "
                        f"{eur(trade['fill_price'])} dans son portefeuille fictif")
                if "realized_pnl" in trade:
                    text += f", résultat {eur(trade['realized_pnl'])}"
                self.strategy.emit("strategy.lab", "coordinator", "lab1d-" + uuid.uuid4().hex[:8], text + ".",
                                   {k: trade[k] for k in ("strategy", "side", "quantity", "fill_price", "fee")}
                                   | {"symbol": symbol, "timeframe": "1d"})
                trades.append(trade)
        return trades

    async def scan(self, ex, exchange_id: str, main_symbol: str) -> list[str]:
        """Choisit les paires suivies. En cas d'échec, on se replie sur la paire
        principale : le scanner ne doit jamais empêcher la surveillance."""
        corr = uuid.uuid4().hex[:12]
        try:
            return await self._use(self.market).scan(
                ex, exchange_id, main_symbol, self.settings.watchlist_size, corr,
                keep=list(self.broker.portfolio.positions))
        except Exception as exc:
            self.market.last_error = str(exc)
            self.emit("agent.error", "coordinator", corr,
                      f"Scanner indisponible ({exc}) : seule {main_symbol} est suivie.",
                      {"agent": "market_intelligence", "error": str(exc)}, status="error")
            return [main_symbol]
        finally:
            self.market.status = "idle"

    async def run_cycle(self, ex, exchange_id: str, symbol: str, timeframe: str) -> str:
        corr = uuid.uuid4().hex[:12]
        if self.bus.halted:
            self.emit("cycle.end", "coordinator", corr,
                      f"Cycle refusé : système à l'arrêt ({self.bus.halt_reason})", status="rejected")
            return corr
        started = perf_counter()
        self.emit("cycle.start", "market_intelligence", corr,
                  f"Nouveau cycle : {symbol} sur {exchange_id} ({timeframe}).",
                  {"exchange": exchange_id, "symbol": symbol, "timeframe": timeframe})
        status, outcome = "ok", "aucun signal, aucun ordre"
        try:
            news = (self.news_source or self.news).blocking.get(exchange_id)
            if not self._use(self.monitor).check(ex, exchange_id, self.broker.portfolio, corr, news):
                raise _Suspended(f"{exchange_id} indisponible")
            self._use(self.market)
            snap = await self.market.collect(ex, exchange_id, symbol, timeframe, corr)
            self.closes[symbol] = [(c[0], float(c[4])) for c in snap.candles]
            self._use(self.regime).detect(snap, corr)
            try:
                self.shadow(exchange_id).update(symbol, snap.candles)
            except Exception as exc:
                self.last_error = f"shadow : {exc}"
            if any(o["symbol"] == symbol for o in self.broker.portfolio.pending.values()):
                self._use(self.execution).check_pending(symbol, snap.candles, corr)
            self._use(self.portfolio).update({symbol: snap.last_price}, corr)
            holding = symbol in self.broker.portfolio.positions
            fill = None
            if holding:
                fill = self._use(self.execution).check_stops(symbol, snap.candles, corr)
                outcome = "position fermée par le stop" if fill else "position conservée"
                if not fill:
                    kind, _ = self._use(self.strategy).analyze(
                        snap, True, corr, self.broker.portfolio.positions[symbol].get("strategy"))
                    if kind == "exit":
                        self._use(self.market)
                        book = await self.market.order_book(ex, symbol, corr)
                        fill = self._use(self.execution).close(
                            symbol, snap.last_price, corr, bids=book["bids"])
                        outcome = "position fermée sur signal de sortie"
            elif self.broker.portfolio.analysed.get(
                    key := f"{exchange_id}:{symbol}:{timeframe}") == snap.candles[-1][0]:
                # Cette bougie a déjà donné lieu à une décision : on ne rejoue pas le signal
                # (évite de racheter après un stop ou un redémarrage sur la même bougie).
                outcome = "bougie déjà analysée, rien de nouveau"
            else:
                kind, signal = self._use(self.strategy).analyze(snap, False, corr)
                self.broker.portfolio.analysed[key] = snap.candles[-1][0]
                self.broker._save()
                if kind == "blocked":
                    self._shade(exchange_id, signal, *self.strategy.last_block, snap.candles[-1][0], corr)
                if kind == "signal":
                    self._use(self.market)
                    book = await self.market.order_book(ex, symbol, corr, target="adversarial_analyst")
                    review = self._use(self.adversary).review(signal, snap, book, corr)
                    outcome = "signal écarté par l'analyse contradictoire"
                    if review["verdict"] != "pass":
                        majors = [o["text"] for o in review["objections"] if o["severity"] == "majeure"]
                        self._shade(exchange_id, signal, "contradicteur", " ; ".join(majors),
                                    snap.candles[-1][0], corr)
                    if review["verdict"] == "pass":
                        proposal = self._use(self.proposer).propose(
                            signal, corr, timeframe, review["objections"])
                        allocation = self._use(self.optimizer).assess(
                            signal, self.broker.portfolio, self.closes, corr)
                        decision = self._use(self.risk).review(signal, self.broker.portfolio, corr, {
                            "data_age_s": snap.data_age_s, "max_age_s": snap.max_age_s,
                            "spread_bps": snap.spread_bps, "min_amount": snap.min_amount,
                            "min_cost": snap.min_cost,
                            "book_age_s": (ex.now_ms() - book["book_ms"]) / 1000,
                            "correlated_open_risk": allocation["correlated_open_risk"]}, proposal)
                        outcome = "proposition refusée par le Risk Manager"
                        if not decision.approved:
                            self._shade(exchange_id, signal, "risque",
                                        RISK_REASONS_FR.get(decision.reason, decision.reason),
                                        snap.candles[-1][0], corr)
                        if decision.approved:
                            fees = ex.fees(symbol) if hasattr(ex, "fees") else {}
                            taker = fees.get("taker") or self.settings.paper_fee_rate
                            maker = fees.get("maker") or taker
                            route = self._use(self.router).plan(decision.order, book, taker, maker, corr)
                            if route["choice"] == "limit":
                                tf_ms = int(snap.max_age_s * 1000)
                                placed = self._use(self.execution).place_limit(
                                    decision.order, route["limit_price"], snap.candles[-1][0],
                                    snap.candles[-1][0] + tf_ms * (1 + self.settings.limit_expiry_candles),
                                    maker, corr, proposal, self.settings.limit_max_participation)
                                outcome = ("ordre limite posé, en attente" if placed.get("status") == "placed"
                                           else "ordre simulé rejeté")
                            else:
                                fill = self._use(self.execution).open(
                                    decision.order, snap.candles[-1][0], corr, asks=book["asks"],
                                    proposal=proposal)
                                outcome = ("achat simulé exécuté" if fill.get("status") == "filled"
                                           else "ordre simulé rejeté")
            if fill and fill.get("status") == "filled":
                self._use(self.portfolio).update({symbol: snap.last_price}, corr)
            if self.lab:
                self._run_lab(snap, corr)
        except _Suspended as exc:
            status, outcome = "rejected", f"aucune opération, {exc}"
            for agent in self.agents:
                if agent.status == "active":
                    agent.status = "idle"
        except Exception as exc:  # un agent en panne ne doit jamais mener à un ordre
            failed = self._current
            failed.last_error = str(exc)
            status, outcome = "error", f"interrompu par une erreur de {failed.name}"
            self.emit("agent.error", "coordinator", corr, f"Erreur de {failed.name} : {exc}",
                      {"agent": failed.name, "error": str(exc)}, status="error")
            for agent in self.agents:
                if agent.status == "active":
                    agent.status = "idle"
            if failed.enabled:
                failed.status = "error"
        else:
            for agent in self.agents:
                if agent.status in ("active", "error"):
                    agent.status = "idle"
        self.emit("cycle.end", "auditor", corr, f"Cycle terminé : {outcome}.",
                  {"outcome": outcome}, status=status, started=started)
        self.auditor.close_thread(corr)
        return corr


def format_event(event: Event) -> str:
    mark = {"ok": " ", "rejected": "✗", "error": "!"}.get(event.status, "?")
    return (f"{event.ts.astimezone().strftime('%H:%M:%S')} {mark} "
            f"{event.source:>19} → {event.target:<19} {event.result}")


async def run_agents(exchange_id: str, symbol: str, timeframe: str = "5m", cycles: int = 1,
                     strategy: str | None = None):
    settings = Settings()
    if strategy:
        settings.strategy = strategy
    settings.validate_safety()
    bus = EventBus(settings.event_log_path, settings.halt_flag_path, settings.database_path)
    bus.subscribe(lambda event: print(format_event(event)))
    async with PublicExchange(exchange_id) as ex:
        market = (await ex.markets()).get(symbol)
        if not market or not market.get("spot") or not market.get("active"):
            raise ValueError(f"{symbol} is not an active Spot market on {exchange_id}")
        taker = ex.fees(symbol).get("taker")
        if taker:
            settings.paper_fee_rate = taker  # vrais frais de la plateforme (ordre au marché)
        print(f"Frais appliqués : {settings.paper_fee_rate:.2%} par ordre ({exchange_id}).")
        broker = PaperBroker(settings.paper_starting_cash, settings.paper_fee_rate,
                             settings.paper_slippage_bps, settings.paper_ledger_path,
                             settings.paper_state_path, settings.paper_max_slippage_bps)
        coordinator = Coordinator(bus, settings, broker)
        for cycle in range(cycles):
            if cycle:
                await asyncio.sleep(seconds_to_next_candle(time.time(), ex.timeframe_seconds(timeframe)))
            corr = await coordinator.run_cycle(ex, exchange_id, symbol, timeframe)
            print(f"— fil {corr} terminé —")
            if bus.halted:
                break


def set_halt(halted: bool, reason: str = "") -> None:
    """Arrêt d'urgence manuel (`halt`) ou reprise (`resume`), tracés dans le journal."""
    settings = Settings()
    bus = EventBus(settings.event_log_path, settings.halt_flag_path, settings.database_path)
    if halted:
        text = f"Arrêt d'urgence manuel : {reason or 'sans motif indiqué'}."
        bus.publish(Event(source="coordinator", target="coordinator", type="system.halt",
                          correlation_id="manual", result=text))
        print(text + " Aucun nouvel ordre ne partira avant `resume`.")
    elif not bus.halted:
        print("Le système n'est pas à l'arrêt.")
    else:
        previous = bus.halt_reason
        bus.publish(Event(source="coordinator", target="coordinator", type="system.resume",
                          correlation_id="manual", result=f"Reprise manuelle après : {previous}"))
        print(f"Système relancé (arrêt précédent : {previous}).")


def print_trace(path: str, correlation_id: str | None = None) -> None:
    events = read_events(path)
    if not events:
        print("Aucun événement enregistré.")
        return
    correlation_id = correlation_id or events[-1].correlation_id
    print(f"Fil {correlation_id}")
    for event in events:
        if event.correlation_id == correlation_id:
            print(format_event(event))


async def learning_cli(args) -> None:
    from trading_agent.evaluate import DEFAULT_BASKET
    from trading_agent.evaluate_cli import load_histories
    from trading_agent.learning import propose
    settings = Settings()
    store = ParamStore(settings.learning_dir)
    if args.action == "list":
        for name, a in store.active().items():
            print(f"Actif {name} : version {a.get('version') or 'origine'} {a.get('params')}")
        for v in store.versions()[:15]:
            print(f"{v['id']}  {v['created_at'][:16]}  {v['strategy']:<20} {v['status']:<9} "
                  f"{(v['best'] or {}).get('params', {})}  {'; '.join(v['reasons'])}")
        return
    bus = EventBus(settings.event_log_path, settings.halt_flag_path, settings.database_path)
    manager = LearningManager(bus, settings)
    if args.action == "propose":
        if not args.strategy:
            raise SystemExit("Indiquez --strategy.")
        async with PublicExchange("bitvavo") as ex:
            histories = await load_histories(ex, DEFAULT_BASKET, "4h", 730)
            proposal = await asyncio.to_thread(propose, args.strategy, histories, settings,
                                               ex.timeframe_seconds("4h"), store.params(args.strategy),
                                               store.trials(args.strategy))
        manager.publish_proposal(proposal, "learn-cli")
        print(bus.events[-1].result)
        return
    key = args.id if args.action == "apply" else args.strategy
    if not key or args.confirm != key:
        raise SystemExit("Confirmation incorrecte : retapez exactement " + ("l'identifiant." if args.action == "apply"
                                                                             else "le nom de la stratégie."))
    try:
        entry = store.apply(key, "ligne de commande") if args.action == "apply" else store.rollback(key, "ligne de commande")
    except (PermissionError, ValueError) as exc:
        raise SystemExit(str(exc))
    bus.publish(Event(source="coordinator", target="strategy_analyst", type="registry.change",
                      correlation_id="manual", priority="high",
                      result=f"Réglage {args.action} en ligne de commande : {entry.get('params') or 'origine'}.",
                      payload={"action": f"learning_{args.action}", "by": "ligne de commande", **entry}))
    print(f"Fait. Réglage actif : {entry.get('params') or 'origine'}. Le serveur l'utilise dès le prochain cycle.")


def resume_strategy(name: str, confirm: str) -> None:
    if confirm != name:
        raise SystemExit("Confirmation incorrecte : retapez exactement le nom de la stratégie.")
    settings = Settings()
    try:
        entry = PauseList(settings.pause_path).resume(name, by="ligne de commande")
    except ValueError as exc:
        raise SystemExit(str(exc))
    bus = EventBus(settings.event_log_path, settings.halt_flag_path, settings.database_path)
    bus.publish(Event(source="coordinator", target="strategy_analyst", type="registry.change",
                      correlation_id="manual", priority="high",
                      result=f"Pause de la stratégie {name} levée en ligne de commande "
                             f"(elle avait été mise en pause pour : {entry['reason']}).",
                      payload={"strategy": name, "action": "resume", "by": "ligne de commande"}))
    print(f"La stratégie {name} peut de nouveau ouvrir des positions.")


def print_db(args) -> None:
    from trading_agent.store import EventStore
    settings = Settings()
    store = EventStore(settings.database_path)
    if args.sync:
        print(f"{store.sync_from_journal(Path(settings.event_log_path))} événement(s) ajouté(s) depuis le journal.")
    if args.type or args.symbol:
        for e in reversed(store.search(type_prefix=args.type, symbol=args.symbol, limit=args.limit)):
            print(f"{e['ts'][:19]}  {e['source']:<19} {e['type']:<22} {e['result']}")
        return
    st = store.stats()
    check = store.compare_with_journal(Path(settings.event_log_path))
    print(f"Base {st['path']} : {st['events']} événements, {st['threads']} fils, "
          f"du {(st['first'] or '-')[:16]} au {(st['last'] or '-')[:16]}.")
    print("Concordance avec le journal : " + ("parfaite." if check["consistent"] else
          f"{check['missing']} manquant(s), {check['extra']} en trop, {check['altered']} différent(s). "
          "Lancez « db --sync » pour rattraper les manquants."))
    for type_, n in list(st["by_type"].items())[:12]:
        print(f"  {type_:<24} {n}")


def print_verify(path: str) -> None:
    intact, count, bad_line = verify_chain(path)
    if intact:
        print(f"Journal intact : {count} événements vérifiés.")
    else:
        print(f"JOURNAL ALTÉRÉ à la ligne {bad_line} ({count} événements valides avant).")
