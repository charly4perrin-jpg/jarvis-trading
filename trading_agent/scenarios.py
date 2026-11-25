"""Simulation de scénarios extrêmes.

Chaque scénario fait tourner les vrais agents contre une plateforme simulée, dans un
dossier temporaire isolé (aucun fichier de l'essai en cours n'est touché), et
vérifie que le système se met en sécurité. Aucun appel réseau, aucun ordre réel.
"""
import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt.async_support as ccxt

from trading_agent.agents import Coordinator
from trading_agent.config import Settings
from trading_agent.events import Event, EventBus
from trading_agent.models import PaperOrder, Side
from trading_agent.paper import PaperBroker
from trading_agent.resilience import ExchangeHealth

MIN = 60_000
BUY = [100.0] * 30 + [130.0]          # croisement haussier : la démo émet un signal


class SimExchange:
    """Plateforme simulée, réglable pour chaque scénario."""

    def __init__(self, closes, book_size=100.0, empty_book=False, fail=None, now_ms=None,
                 candles=None, health=None, book_ts_ms=None):
        self.closes, self.book_size, self.empty_book = closes, book_size, empty_book
        self.book_ts_ms = book_ts_ms     # heure du carnet donnée par la « plateforme » (facultative)
        self.fail, self._now, self._candles = fail, now_ms, candles
        if health:
            self.health = health

    async def _call(self, name, factory):
        if hasattr(self, "health"):
            return await self.health.call(name, factory)
        return await factory()

    async def ohlcv(self, symbol, timeframe="1m", limit=100, since=None):
        async def go():
            if self.fail:
                raise self.fail
            return self._candles or [[i * MIN, c, c + 1, c - 1, c, 1.0] for i, c in enumerate(self.closes)]
        return await self._call("ohlcv", go)

    async def ticker(self, symbol):
        last = self.closes[-1]
        return {"bid": last - 0.05, "ask": last + 0.05}

    async def order_book(self, symbol, limit=50):
        last = self.closes[-1]
        if self.empty_book:
            return {"bids": [], "asks": []}
        book = {"bids": [[last - 0.05, self.book_size]], "asks": [[last + 0.05, self.book_size]]}
        if self.book_ts_ms is not None:
            book["timestamp"] = self.book_ts_ms
        return book

    def limits(self, symbol):
        return {"min_amount": 0.0001, "min_cost": 5.0}

    def timeframe_seconds(self, timeframe):
        return 60

    def now_ms(self):
        return self._now if self._now is not None else len(self._candles or self.closes) * MIN


def _setup(tmp: Path, **kw):
    settings = Settings(
        _env_file=None, **{"regime_filter": False, **kw}, event_log_path=str(tmp / "events.jsonl"),
        halt_flag_path=str(tmp / "HALT"), database_path=str(tmp / "jarvis.db"),
        pause_path=str(tmp / "paused.json"), performance_dir=str(tmp / "perf"),
        evaluations_dir=str(tmp / "ev"), arbitrage_log_path=str(tmp / "arb.jsonl"),
        registry_path=str(tmp / "registry.json"), lab_dir=str(tmp / "lab"),
        # Sinon ces dossiers retombent sur ceux de l'essai principal (data/shadow…).
        shadow_dir=str(tmp / "shadow"), learning_dir=str(tmp / "learning"),
        breakers_dir=str(tmp / "breakers"), lab_daily_dir=str(tmp / "lab_1d"),
        ai_cache_path=str(tmp / "ai_cache.json"), ai_budget_path=str(tmp / "ai_budget.json"))
    bus = EventBus(settings.event_log_path, settings.halt_flag_path, settings.database_path)
    broker = PaperBroker(1000.0, 0.0025, 10, str(tmp / "ledger.jsonl"), str(tmp / "state.json"))
    return settings, bus, broker, Coordinator(bus, settings, broker)


def _types(bus, corr):
    return [e.type for e in bus.thread(corr)]


async def flash_crash(tmp):
    _, bus, broker, c = _setup(tmp)
    broker.portfolio.positions["BTC/EUR"] = {
        "quantity": 1.0, "entry_price": 100.0, "mark_price": 100.0, "stop_price": 95.0,
        "entry_fee": 0.25, "checked_ms": 0, "strategy": "momentum_demo"}
    broker.portfolio.cash -= 100.25
    broker.mark({})       # capital recalculé : sinon le contrôle de cohérence arrête tout
    candles = [[i * MIN, 100.0, 101.0, 99.0, 100.0, 1.0] for i in range(40)]
    candles.append([40 * MIN, 70.0, 71.0, 65.0, 68.0, 50.0])      # ouverture 30 % sous le stop
    corr = await c.run_cycle(SimExchange([68.0] * 41, candles=candles), "sim", "BTC/EUR", "1m")
    fill = next((e for e in bus.thread(corr) if e.type == "execution.fill"), None)
    ok = (fill is not None and fill.payload.get("reason") == "stop"
          and fill.payload["fill_price"] <= 70.0 and "BTC/EUR" not in broker.portfolio.positions)
    return ok, (f"position vendue à {fill.payload['fill_price']:.2f} (ouverture), pas au stop théorique de 95"
                if fill else "aucune vente : la position est restée ouverte")


async def extreme_volatility(tmp):
    _, bus, broker, c = _setup(tmp, regime_filter=True)
    closes = [100 + (i % 2) * 0.2 for i in range(260)] + [100, 135]   # choc sur la dernière bougie
    corr = await c.run_cycle(SimExchange(closes), "sim", "BTC/EUR", "1m")
    regime = next(e for e in bus.thread(corr) if e.type == "regime.detected").payload["regime"]
    types = _types(bus, corr)
    ok = regime and regime["shock"] and "trade.proposal" not in types and not broker.portfolio.positions
    return ok, (f"mouvement exceptionnel détecté (stress : {regime and regime['shock']}), aucune proposition"
                if ok else f"comportement inattendu : {types}")


async def empty_book(tmp):
    _, bus, broker, c = _setup(tmp)
    corr = await c.run_cycle(SimExchange(BUY, empty_book=True), "sim", "BTC/EUR", "1m")
    ok = not broker.portfolio.positions and "execution.fill" not in _types(bus, corr)
    end = next(e for e in bus.thread(corr) if e.type == "cycle.end")
    return ok, f"aucun achat ; fin du cycle : {end.result}"


async def thin_liquidity(tmp):
    _, bus, broker, c = _setup(tmp)
    corr = await c.run_cycle(SimExchange(BUY, book_size=0.5), "sim", "BTC/EUR", "1m")
    review = next((e for e in bus.thread(corr) if e.type == "adversarial.review"), None)
    ok = review is not None and review.payload["verdict"] == "reject" and not broker.portfolio.positions
    return ok, review.result if review else "pas d'analyse contradictoire"


async def disconnection(tmp):
    _, bus, broker, c = _setup(tmp)

    async def no_sleep(_):
        return None
    health = ExchangeHealth("sim", retries=1, failure_threshold=2, cooldown_s=300, sleep=no_sleep)
    ex = SimExchange(BUY, fail=ccxt.NetworkError("connexion perdue"), health=health)
    await c.run_cycle(ex, "sim", "BTC/EUR", "1m")
    corr = await c.run_cycle(ex, "sim", "BTC/EUR", "1m")
    status = next(e for e in bus.thread(corr) if e.type == "exchange.status")
    ok = (not status.payload["tradable"] and not bus.halted and not broker.portfolio.positions
          and "market.snapshot" not in _types(bus, corr))
    return ok, f"plateforme suspendue sans arrêter le système : {status.result}"


async def repeated_errors(tmp):
    _, bus, broker, c = _setup(tmp)
    ex = SimExchange(BUY, fail=RuntimeError("réponse illisible"))
    for _ in range(5):
        await c.run_cycle(ex, "sim", "BTC/EUR", "1m")
    return bus.halted, (f"arrêt d'urgence déclenché : {bus.halt_reason}" if bus.halted
                        else "le système ne s'est pas arrêté")


async def stale_data(tmp):
    _, bus, broker, c = _setup(tmp)
    corr = await c.run_cycle(SimExchange(BUY, now_ms=len(BUY) * MIN + 3_600_000), "sim", "BTC/EUR", "1m")
    risk = next((e for e in bus.thread(corr) if e.type == "risk.decision"), None)
    ok = risk is not None and risk.status == "rejected" and not broker.portfolio.positions
    return ok, risk.result if risk else "pas de décision du risque"


async def duplicate_and_expired(tmp):
    _, bus, broker, c = _setup(tmp)
    corr = await c.run_cycle(SimExchange(BUY), "sim", "BTC/EUR", "1m")
    proposal = next(e for e in bus.thread(corr) if e.type == "trade.proposal").payload
    order = PaperOrder("BTC/EUR", Side.BUY, 0.1, 130.0, 120.0)
    again = c.execution.open(order, 0, corr, proposal=proposal)
    old = {**proposal, "proposal_id": "ancienne",
           "valid_until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
    bus.publish(Event(source="risk_manager", target="execution", type="risk.decision", correlation_id=corr,
                      payload={"approved": True, "proposal_id": "ancienne"}))
    late = c.execution.open(order, 0, corr, proposal=old)
    ok = again["status"] == late["status"] == "rejected" and len(broker.portfolio.positions) == 1
    return ok, f"rejoué : « {again['reason']} » ; expiré : « {late['reason']} »"


async def database_outage(tmp):
    import sqlite3
    _, bus, broker, c = _setup(tmp)

    def broken(event):
        raise sqlite3.OperationalError("disque plein")
    bus.store.insert = broken
    corr = await c.run_cycle(SimExchange(BUY), "sim", "BTC/EUR", "1m")
    rejected = next((e for e in bus.thread(corr) if e.type == "execution.rejected"), None)
    ok = rejected is not None and not broker.portfolio.positions
    return ok, rejected.result if rejected else "achat exécuté malgré la panne"


async def halt_with_pending_order(tmp):
    _, bus, broker, c = _setup(tmp)
    broker.place_limit(PaperOrder("BTC/EUR", Side.BUY, 1.0, 100.0, 95.0), 99.0, 0, MIN, 0.0015)
    c.halt("scénario")
    ok = not broker.portfolio.pending and bus.halted
    return ok, "ordre limite annulé immédiatement" if ok else "ordre toujours en attente"


SCENARIOS = [
    ("Krach éclair sous le stop", "vendre au prix réellement disponible, pas au stop théorique", flash_crash),
    ("Volatilité extrême", "aucune entrée pendant un mouvement exceptionnel", extreme_volatility),
    ("Carnet d'ordres vide", "aucun achat", empty_book),
    ("Liquidité insuffisante", "objection majeure du contradicteur, aucun achat", thin_liquidity),
    ("Déconnexion de la plateforme", "suspendre cette plateforme sans tout arrêter", disconnection),
    ("Erreurs répétées", "arrêt d'urgence après 5 cycles en erreur", repeated_errors),
    ("Données périmées", "refus du moteur de risque", stale_data),
    ("Message dupliqué et proposition expirée", "aucune double exécution, aucune exécution tardive",
     duplicate_and_expired),
    ("Base de données en panne", "aucun achat sans enregistrement complet", database_outage),
    ("Arrêt d'urgence avec un ordre en attente", "annulation immédiate de l'ordre", halt_with_pending_order),
]


async def run_all() -> dict:
    results = []
    for name, expected, fn in SCENARIOS:
        with tempfile.TemporaryDirectory(prefix="jarvis-scenario-") as tmp:
            try:
                ok, observed = await fn(Path(tmp))
            except Exception as exc:  # un scénario qui plante est un échec, pas un crash du serveur
                ok, observed = False, f"erreur pendant le scénario : {type(exc).__name__} {exc}"
        results.append({"name": name, "expected": expected, "passed": bool(ok), "observed": observed})
    return {"at": datetime.now(timezone.utc).isoformat(), "results": results,
            "passed": sum(r["passed"] for r in results), "total": len(results)}


def save(report: dict, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


def main(path: str) -> None:
    report = asyncio.run(run_all())
    save(report, path)
    for r in report["results"]:
        print(f"[{'OK ' if r['passed'] else 'NON'}] {r['name']} — attendu : {r['expected']}\n      observé : {r['observed']}")
    print(f"{report['passed']} scénario(s) sur {report['total']} réussi(s).")
