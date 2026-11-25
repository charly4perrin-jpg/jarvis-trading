"""Serveur local de l'interface Jarvis.

Il fait tourner le cycle des agents en arrière-plan et expose :
- GET  /            la page (carte des agents + fil de décision)
- GET  /api/state   état des agents, liaisons, portefeuille, système
- GET  /api/events  derniers événements du journal
- GET  /api/stream  événements en direct (Server-Sent Events)
- POST /api/cycle   lance un cycle tout de suite
- POST /api/halt    arrêt d'urgence     POST /api/resume   reprise

Tout ce que la page affiche vient des événements réels du bus. Le serveur
n'écoute que sur 127.0.0.1 (cette machine uniquement).
"""
import asyncio
from datetime import datetime
import os
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from trading_agent.agents import Coordinator
from trading_agent.config import Settings
from trading_agent.events import Event, EventBus, read_events, verify_chain
from trading_agent.exchanges import PublicExchange
from trading_agent.backtest import compare_strategies, fetch_history
from trading_agent.evaluate import (DEFAULT_BASKET, STATUS_LABELS, Registry, paper_evidence)
from trading_agent.evaluate_cli import evaluate_all
from trading_agent.lab import Lab
from trading_agent.notifications import Notifier, combine, email_sender, send_macos
from trading_agent.paper import PaperBroker
from trading_agent.runner import seconds_to_next_candle
from trading_agent.strategies import STRATEGIES, get_strategy

WEB_DIR = Path(__file__).parent / "web"
HISTORY_LIMIT = 50_000  # plusieurs années de cycles en bougies 4h

LABELS = {
    "coordinator": "Coordinateur", "market_intelligence": "Marché",
    "regime_detector": "Régime", "adversarial_analyst": "Contradicteur",
    "exchange_monitor": "Plateformes", "portfolio_optimizer": "Allocation",
    "performance_analyst": "Performance", "execution_intelligence": "Routage",
    "arbitrage_analyst": "Arbitrage", "learning_manager": "Apprentissage",
    "news_analyst": "Actualités", "ai_analyst": "IA",
    "strategy_analyst": "Stratégie", "trade_proposer": "Proposition",
    "risk_manager": "Risque", "execution": "Exécution",
    "portfolio_manager": "Portefeuille", "security_monitor": "Sécurité",
    "auditor": "Audit",
}

# Liaisons prévues par l'architecture (affichées en pointillés tant qu'aucun
# message réel n'y est passé).
EXPECTED_LINKS = [
    ("coordinator", "market_intelligence"), ("market_intelligence", "regime_detector"),
    ("regime_detector", "strategy_analyst"), ("strategy_analyst", "adversarial_analyst"),
    ("market_intelligence", "adversarial_analyst"), ("adversarial_analyst", "trade_proposer"),
    ("adversarial_analyst", "coordinator"), ("market_intelligence", "coordinator"),
    ("trade_proposer", "portfolio_optimizer"), ("portfolio_optimizer", "risk_manager"),
    ("exchange_monitor", "coordinator"), ("risk_manager", "execution"),
    ("performance_analyst", "coordinator"), ("risk_manager", "execution_intelligence"),
    ("execution_intelligence", "execution"), ("arbitrage_analyst", "coordinator"),
    ("learning_manager", "coordinator"), ("news_analyst", "exchange_monitor"),
    ("ai_analyst", "coordinator"),
    ("execution", "portfolio_manager"), ("portfolio_manager", "auditor"),
    ("strategy_analyst", "coordinator"), ("risk_manager", "coordinator"),
    ("execution", "coordinator"), ("coordinator", "auditor"),
    ("auditor", "coordinator"), ("security_monitor", "coordinator"),
]


# Secrets ET données personnelles : jamais affichés ni écrits dans le journal.
SECRET_WORDS = ("key", "secret", "password", "token", "passphrase", "email", "smtp_user", "phone")


def public_config(settings: Settings) -> dict:
    """Paramètres affichables : tout sauf ce qui ressemble à un secret."""
    return {k: v for k, v in settings.model_dump().items()
            if not any(word in k.lower() for word in SECRET_WORDS)}


def config_fingerprint(config: dict) -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()[:16]


class ExplainRequest(BaseModel):
    thread: str


class LearningRequest(BaseModel):
    strategy: str | None = None
    id: str | None = None


class ResumeStrategyRequest(BaseModel):
    strategy: str


class HaltRequest(BaseModel):
    reason: str = ""
    flatten: bool = False       # fermer aussi toutes les positions simulées


class EvaluateRequest(BaseModel):
    symbols: list[str] = DEFAULT_BASKET
    timeframe: str = "4h"
    days: int = 730


class PromoteRequest(BaseModel):
    strategy: str


class BacktestRequest(BaseModel):
    symbol: str
    timeframe: str = "4h"
    days: int = 365


LAB_TIMEFRAMES = ("1h", "4h", "1d")
CURVE_POINTS = 240


def thin(values: list, points: int = CURVE_POINTS) -> list:
    """Réduit une courbe à quelques centaines de points pour l'affichage."""
    if len(values) <= points:
        return values
    step = (len(values) - 1) / (points - 1)
    return [values[round(i * step)] for i in range(points)]


def create_app(exchange_id: str, symbol: str, timeframe: str = "5m",
               interval: float | None = None, settings: Settings | None = None,
               exchange_factory=PublicExchange, auto_run: bool = True) -> FastAPI:
    settings = settings or Settings()
    settings.validate_safety()
    hub = {"history": [], "clients": set(), "coordinator": None, "broker": None,
           "trigger": asyncio.Event(), "next_cycle_at": None, "interval": interval,
           "running": False, "ex": None, "lab": None, "backtesting": False,
           "notifier": None, "watchlist": [symbol], "venues": [],
           "registry": Registry(settings.registry_path, settings.evaluations_dir)}

    def on_event(event: Event) -> None:
        hub["history"].append(event)
        del hub["history"][:-HISTORY_LIMIT]
        for queue in hub["clients"]:
            queue.put_nowait(event)

    async def cycle_loop(ex) -> None:
        coordinator = hub["coordinator"]
        first = True
        while True:
            # auto_run=False : aucun cycle automatique, seulement sur demande.
            if auto_run and coordinator.bus.halted:
                # Arrêt d'urgence : plus d'analyse ni d'achat, mais les stops restent surveillés.
                hub["running"] = True
                try:
                    for venue in hub["venues"]:
                        await venue["coordinator"].watch_stops(venue["ex"], venue["id"], timeframe)
                except Exception as exc:
                    coordinator.last_error = f"surveillance des stops : {exc}"
                finally:
                    hub["running"] = False
            elif (auto_run or not first) and not coordinator.bus.halted:
                hub["running"] = True
                try:
                    # Actualités officielles d'abord : un incident bloquant suspend la plateforme.
                    if settings.news_enabled:
                        try:
                            corr_news = "news-" + uuid.uuid4().hex[:8]
                            await coordinator.news.check({v["id"]: v["watchlist"] for v in hub["venues"]}, corr_news)
                            await coordinator.ai.interpret_news(coordinator.news.latest, corr_news)
                        except Exception as exc:
                            coordinator.news.last_error = str(exc)
                        try:   # calendrier économique : affiché, jamais lu par les décisions
                            coordinator.news.check_calendar("calendar-" + uuid.uuid4().hex[:8])
                        except Exception as exc:
                            coordinator.news.calendar = {"error": f"calendrier : {exc}", "upcoming": []}
                    # Une plateforme après l'autre ; une plateforme suspendue n'arrête pas les autres.
                    for venue in hub["venues"]:
                        venue["coordinator"].news_source = coordinator.news
                        if time.time() >= venue["next_scan_at"]:
                            venue["watchlist"] = await venue["coordinator"].scan(
                                venue["ex"], venue["id"], venue["symbol"])
                            venue["next_scan_at"] = time.time() + settings.scan_every_hours * 3600
                        for pair in venue["watchlist"]:
                            if coordinator.bus.halted:
                                break
                            await venue["coordinator"].run_cycle(venue["ex"], venue["id"], pair, timeframe)
                    hub["watchlist"] = hub["venues"][0]["watchlist"]
                    if not coordinator.bus.halted and timeframe != "1d":
                        await coordinator.run_daily_lab(hub["lab_1d"], ex, exchange_id, hub["watchlist"])
                    if not coordinator.bus.halted:
                        try:
                            coordinator.performance.report(hub["venues"], hub["tf_seconds"],
                                                           "perf-" + uuid.uuid4().hex[:8])
                        except Exception as exc:  # la mesure ne doit jamais arrêter les cycles
                            coordinator.performance.last_error = str(exc)
                        try:
                            await coordinator.arbitrage.scan(hub["venues"], "arb-" + uuid.uuid4().hex[:8])
                        except Exception as exc:  # observation seulement : jamais bloquant
                            coordinator.arbitrage.last_error = str(exc)
                        try:
                            coordinator.learning.review(coordinator.performance.last,
                                                        coordinator.performance.pauses.all(),
                                                        "learn-" + uuid.uuid4().hex[:8])
                        except Exception as exc:
                            coordinator.learning.last_error = str(exc)
                finally:
                    hub["running"] = False
            first = False
            hub["trigger"].clear()
            if not auto_run:
                await hub["trigger"].wait()
                continue
            # Sans intervalle imposé, on attend la prochaine clôture de bougie.
            wait = hub["interval"] or seconds_to_next_candle(time.time(), hub["tf_seconds"])
            hub["next_cycle_at"] = time.time() + wait
            try:
                await asyncio.wait_for(hub["trigger"].wait(), wait)
            except asyncio.TimeoutError:
                pass

    def take_lock() -> Path:
        """Un seul serveur par dossier de données : deux serveurs écriraient dans le
        même portefeuille et casseraient la chaîne du journal."""
        lock = Path(settings.event_log_path).with_name("server.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        if lock.exists():
            try:
                pid = int(lock.read_text().strip())
                os.kill(pid, 0)
            except (ValueError, ProcessLookupError):
                pass  # verrou laissé par un serveur qui ne tourne plus
            except PermissionError:
                raise RuntimeError(f"Un serveur Jarvis tourne déjà (processus {pid}).")
            else:
                if pid != os.getpid():
                    raise RuntimeError(f"Un serveur Jarvis tourne déjà (processus {pid}).")
        lock.write_text(str(os.getpid()))
        return lock

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lock = take_lock()
        hub["history"] = read_events(settings.event_log_path)[-HISTORY_LIMIT:]
        bus = EventBus(settings.event_log_path, settings.halt_flag_path, settings.database_path)
        bus.subscribe(on_event)
        async with AsyncExitStack() as stack:
            ex = await stack.enter_async_context(exchange_factory(exchange_id))
            if hasattr(ex, "health"):
                ex.health.persist_to(str(Path(settings.breakers_dir) / f"{exchange_id}.json"))
            market = (await ex.markets()).get(symbol)
            if not market or not market.get("spot") or not market.get("active"):
                raise ValueError(f"{symbol} is not an active Spot market on {exchange_id}")
            taker = ex.fees(symbol).get("taker")
            if taker:
                settings.paper_fee_rate = taker
            hub["tf_seconds"] = ex.timeframe_seconds(timeframe)
            hub["broker"] = PaperBroker(
                settings.paper_starting_cash, settings.paper_fee_rate,
                settings.paper_slippage_bps, settings.paper_ledger_path,
                settings.paper_state_path, settings.paper_max_slippage_bps)
            hub["coordinator"] = Coordinator(bus, settings, hub["broker"])
            hub["ex"] = ex
            hub["email"] = email_sender(settings)
            hub["notifier"] = Notifier(
                settings.notifications_path,
                sender=combine(send_macos if settings.notify_macos else None, hub["email"]),
                portfolio=lambda: hub["broker"].portfolio,
                daily_summary=settings.notify_daily_summary)
            bus.subscribe(hub["notifier"].on_event)
            # Cycle principal en bougies d'un jour : un seul laboratoire journalier (pas de doublon).
            daily_main = timeframe == "1d"
            hub["lab_1d"] = Lab(settings, settings.lab_daily_dir)
            hub["lab"] = hub["coordinator"].lab = hub["lab_1d"] if daily_main else Lab(settings, settings.lab_dir)
            hub["venues"] = [{"id": exchange_id, "ex": ex, "symbol": symbol, "broker": hub["broker"],
                              "start": settings.paper_starting_cash,
                              "coordinator": hub["coordinator"], "watchlist": [symbol],
                              "next_scan_at": 0.0}]
            for extra in settings.extra_exchanges:
                # Chaque plateforme en plus : son propre portefeuille fictif et ses frais,
                # même bus, même journal, même Security Monitor.
                if extra == exchange_id:
                    continue
                other = await stack.enter_async_context(exchange_factory(extra))
                if hasattr(other, "health"):
                    other.health.persist_to(str(Path(settings.breakers_dir) / f"{extra}.json"))
                markets = await other.markets()
                main = symbol if (markets.get(symbol) or {}).get("spot") else None
                folder = Path(settings.paper_state_path).parent / extra
                local = settings.model_copy(update={
                    "paper_fee_rate": other.fees(main).get("taker") or settings.paper_fee_rate
                    if main else settings.paper_fee_rate,
                    "watchlist_size": settings.extra_watchlist_size})
                broker = PaperBroker(settings.extra_exchange_cash, local.paper_fee_rate,
                                     settings.paper_slippage_bps, str(folder / "paper_ledger.jsonl"),
                                     str(folder / "paper_state.json"), settings.paper_max_slippage_bps)
                hub["venues"].append({
                    "id": extra, "ex": other, "symbol": main or symbol, "broker": broker,
                    "start": settings.extra_exchange_cash,
                    "coordinator": Coordinator(bus, local, broker, security=hub["coordinator"].security),
                    "watchlist": [main] if main else [], "next_scan_at": 0.0})
            # Configuration versionnée : son empreinte est inscrite au journal à chaque
            # démarrage, et tout changement depuis le démarrage précédent est signalé.
            config = public_config(settings)
            hub["config_fingerprint"] = fingerprint = config_fingerprint(config)
            previous = next((e for e in reversed(hub["history"]) if e.type == "config.loaded"), None)
            changed = previous is not None and previous.payload.get("fingerprint") != fingerprint
            diff = sorted(k for k in config if previous is not None
                          and previous.payload.get("config", {}).get(k) != config[k]) if changed else []
            bus.publish(Event(
                source="coordinator", target="auditor", type="config.loaded", correlation_id="manual",
                priority="high" if changed else "low",
                result=(f"Configuration {fingerprint} chargée"
                        + (f" : modifiée depuis le démarrage précédent ({', '.join(diff)})." if changed
                           else " : identique au démarrage précédent." if previous else ".")),
                payload={"fingerprint": fingerprint, "config": config, "changed": diff}))
            task = asyncio.create_task(cycle_loop(ex))
            try:
                yield
            finally:
                task.cancel()
                lock.unlink(missing_ok=True)

    app = FastAPI(title="Jarvis", lifespan=lifespan)

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    VENDOR = {"lightweight-charts.js": "lightweight-charts.standalone.production.js"}

    @app.get("/vendor/{name}")
    async def vendor(name: str):
        # Liste fermée : seuls les fichiers prévus sont servis (aucun chemin libre).
        if name not in VENDOR:
            raise HTTPException(404, "Fichier inconnu.")
        return FileResponse(WEB_DIR / "vendor" / VENDOR[name], media_type="text/javascript")

    @app.get("/api/chart")
    async def chart(exchange: str, symbol: str, limit: int = 300):
        """Bougies closes d'une paire, avec les achats et ventes simulés de Jarvis dessus."""
        from trading_agent.runner import closed_candles
        venue = next((v for v in hub["venues"] if v["id"] == exchange), None)
        if venue is None:
            raise HTTPException(404, "Plateforme inconnue.")
        ex, tf_ms = venue["ex"], hub["tf_seconds"] * 1000
        try:
            raw = await ex.ohlcv(symbol, timeframe=timeframe, limit=max(30, min(limit, 1000)))
        except Exception as exc:
            raise HTTPException(503, f"Bougies indisponibles : {type(exc).__name__}.")
        candles = closed_candles(raw, tf_ms, ex.now_ms())
        markers = []
        store = hub["coordinator"].bus.store
        if store:
            for e in store.search(type_prefix="execution.fill", symbol=symbol, limit=500):
                start = store.search(correlation_id=e["correlation_id"], type_prefix="cycle.start", limit=1)
                if not start or start[0]["payload"].get("exchange") != exchange:
                    continue
                p = e["payload"]
                when = int(datetime.fromisoformat(e["ts"].replace("Z", "+00:00")).timestamp() * 1000)
                markers.append({"time": (when - when % tf_ms) // 1000, "side": p.get("side"),
                                "price": p.get("fill_price"), "pnl": p.get("realized_pnl"),
                                "reason": p.get("reason"), "thread": e["correlation_id"]})
        portfolio = venue["broker"].portfolio
        position = portfolio.positions.get(symbol)
        return {"exchange": exchange, "symbol": symbol, "timeframe": timeframe,
                "candles": [{"time": c[0] // 1000, "open": float(c[1]), "high": float(c[2]),
                             "low": float(c[3]), "close": float(c[4])} for c in candles],
                "markers": sorted(markers, key=lambda m: m["time"]),
                "position": position and {"entry": position["entry_price"], "stop": position["stop_price"]},
                "pending": [{"limit": o["limit_price"], "stop": o["stop_price"]}
                            for o in portfolio.pending.values() if o["symbol"] == symbol]}

    @app.get("/api/state")
    async def state():
        coordinator, portfolio = hub["coordinator"], hub["broker"].portfolio
        links: dict[tuple, dict] = {
            pair: {"source": pair[0], "target": pair[1], "count": 0, "last_ts": None}
            for pair in EXPECTED_LINKS}
        for event in hub["history"]:
            if event.source == event.target:
                continue
            link = links.setdefault((event.source, event.target), {
                "source": event.source, "target": event.target, "count": 0, "last_ts": None})
            link["count"] += 1
            link["last_ts"] = event.ts
        return {
            "agents": [{**agent.describe(), "label": LABELS.get(agent.name, agent.name)}
                       for agent in coordinator.agents],
            "links": list(links.values()),
            "strategies": {name: strat.label for name, strat in STRATEGIES.items()},
            "portfolio": {"equity": portfolio.equity, "cash": portfolio.cash,
                          "daily_pnl": portfolio.daily_pnl, "weekly_pnl": portfolio.weekly_pnl,
                          "positions": portfolio.positions},
            "system": {"halted": coordinator.bus.halted, "halt_reason": coordinator.bus.halt_reason,
                       "exchange": exchange_id, "symbol": symbol, "timeframe": timeframe,
                       "watchlist": hub["watchlist"],
                       "ai": hub["coordinator"].ai.client.usage(),
                       "paused": hub["coordinator"].performance.pauses.all(),
                       "venues": [{"exchange": v["id"], "watchlist": v["watchlist"],
                                   "equity": v["broker"].portfolio.equity,
                                   "cash": v["broker"].portfolio.cash,
                                   "positions": len(v["broker"].portfolio.positions),
                                   "fee_rate": v["coordinator"].settings.paper_fee_rate,
                                   "health": getattr(v["ex"], "health", None) and v["ex"].health.snapshot()}
                                  for v in hub.get("venues", [])],
                       "interval": hub["interval"], "next_cycle_at": hub["next_cycle_at"],
                       "running": hub["running"], "fee_rate": settings.paper_fee_rate,
                       "mode": settings.app_env,
                       "strategy": get_strategy(settings.strategy).label},
        }

    @app.get("/api/events")
    async def events(limit: int = HISTORY_LIMIT):
        return hub["history"][-max(1, min(limit, HISTORY_LIMIT)):]

    @app.get("/api/notifications/status")
    async def notifications_status():
        email = hub.get("email")
        return {"macos": settings.notify_macos, "email": bool(email),
                "email_last_error": email.last_error if email else None}

    @app.post("/api/notifications/test")
    async def notifications_test():
        """Envoie une alerte de test sur tous les canaux configurés (Mac, e-mail)."""
        hub["notifier"].notify("test", "important", "Test des alertes",
                               "Si tu lis ce message, les alertes importantes de Jarvis te parviendront ici.")
        return {"sent": True, "email": bool(hub.get("email"))}

    @app.get("/api/notifications")
    async def notifications(limit: int = 100):
        return hub["notifier"].items[-max(1, min(limit, 500)):][::-1]

    @app.get("/api/lab")
    async def lab_standings(tf: str = "principal"):
        daily = tf == "1d"
        lab = hub["lab_1d"] if daily else hub["lab"]
        return {"since": lab.since, "error": lab.last_error, "symbol": symbol,
                "timeframe": "1d" if daily else timeframe, "start_equity": settings.paper_starting_cash,
                "standings": lab.standings(settings.strategy)}

    @app.get("/api/tearsheet")
    async def tearsheet_reports(tf: str = "principal"):
        """Rapport de performance détaillé, en lecture seule. Une source douteuse donne une
        erreur affichée, jamais un chiffre deviné."""
        from datetime import timezone
        from trading_agent import tearsheet
        from trading_agent.strategies import LIVE_STRATEGIES as STRATEGIES
        now = datetime.now(timezone.utc)
        lab = hub.get("lab_1d") if tf == "1d" else hub.get("lab")
        lab_out = {}
        for name in (STRATEGIES if lab is not None else []):
            try:
                lab_out[name] = tearsheet.lab_report(lab.dir, name, settings.paper_starting_cash, now)
            except Exception as exc:
                lab_out[name] = {"name": name, "error": str(exc)}
        shadow_out = {}
        for venue in hub["venues"]:
            path = Path(settings.shadow_dir) / f"{venue['id']}.json"
            try:
                shadow_out[venue["id"]] = tearsheet.shadow_report(
                    path, settings.paper_starting_cash, settings.risk_per_trade_pct, now)
            except Exception as exc:
                shadow_out[venue["id"]] = {"name": "shadow", "error": str(exc)}
        return {"timeframe": "1d" if tf == "1d" else timeframe, "lab": lab_out, "shadow": shadow_out}

    robustness_cache: dict[str, tuple[float, dict]] = {}
    lookahead_state: dict = {"at": 0.0, "result": None, "lock": asyncio.Lock()}

    @app.get("/api/lookahead")
    async def lookahead_check():
        """Analyse lookahead des stratégies sur l'historique public de la paire principale.
        Lecture seule, calcul dans un fil à part (quelques secondes), gardé 6 heures."""
        from trading_agent.lookahead import analyze_all
        from trading_agent.runner import closed_candles
        async with lookahead_state["lock"]:
            if lookahead_state["result"] and time.time() - lookahead_state["at"] < 6 * 3600:
                return lookahead_state["result"]
            if not hub["venues"]:
                return {"status": "insuffisant", "error": "plateforme pas encore connectée", "reports": {}}
            venue = hub["venues"][0]
            try:
                ex = venue["ex"]
                raw = await ex.ohlcv(venue["symbol"], timeframe=timeframe, limit=600)
                candles = closed_candles(raw, ex.timeframe_seconds(timeframe) * 1000, ex.now_ms())
                full = await asyncio.to_thread(analyze_all, candles)
            except Exception as exc:
                return {"status": "erreur", "error": f"analyse impossible : {exc}", "reports": {}}
            reports = {name: {"status": r["status"],
                              "reason": r.get("reason") or "; ".join(
                                  f"{part} : {r[part]['reason']}" for part in ("entry", "exit") if part in r)}
                       for name, r in full["reports"].items()}
            result = {"status": full["status"], "error": None, "symbol": venue["symbol"],
                      "timeframe": timeframe, "candles": len(candles), "computed_at": datetime.now().isoformat(),
                      "reports": reports}
            lookahead_state.update(at=time.time(), result=result)
            return result

    recursive_state: dict = {"at": 0.0, "result": None, "lock": asyncio.Lock()}

    @app.get("/api/recursive")
    async def recursive_check():
        """Stabilité des signaux selon l'historique disponible (200 à 1000 bougies), sur
        l'historique public de la paire principale. Lecture seule, calcul à part, gardé 6 h."""
        from trading_agent.recursive import analyze_all as recursive_all
        from trading_agent.runner import closed_candles
        async with recursive_state["lock"]:
            if recursive_state["result"] and time.time() - recursive_state["at"] < 6 * 3600:
                return recursive_state["result"]
            if not hub["venues"]:
                return {"status": "insuffisant", "error": "plateforme pas encore connectée", "reports": {}}
            venue = hub["venues"][0]
            try:
                ex = venue["ex"]
                raw = await ex.ohlcv(venue["symbol"], timeframe=timeframe, limit=1100)
                candles = closed_candles(raw, ex.timeframe_seconds(timeframe) * 1000, ex.now_ms())
                full = await asyncio.to_thread(recursive_all, candles)
            except Exception as exc:
                return {"status": "erreur", "error": f"analyse impossible : {exc}", "reports": {}}
            result = {"status": full["status"], "error": None, "symbol": venue["symbol"],
                      "timeframe": timeframe, "candles": len(candles),
                      "computed_at": datetime.now().isoformat(), "reports": full["reports"]}
            recursive_state.update(at=time.time(), result=result)
            return result

    @app.get("/api/robustness")
    async def robustness(tf: str = "principal"):
        """Robustesse Monte-Carlo, en lecture seule. Calcul dans un fil à part (jamais dans
        la boucle du serveur), gardé 10 minutes. Données douteuses : « non concluant »."""
        from trading_agent.monte_carlo import lab_robustness
        key = "1d" if tf == "1d" else "principal"
        cached = robustness_cache.get(key)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        lab = hub.get("lab_1d") if key == "1d" else hub.get("lab")
        if lab is None:
            return {"error": "laboratoire pas encore démarré", "rows": []}
        try:
            rows = await asyncio.to_thread(lab_robustness, str(lab.dir), settings.paper_starting_cash)
            result = {"error": None, "rows": rows, "computed_at": datetime.now().isoformat()}
        except Exception as exc:
            return {"error": f"calcul impossible : {exc}", "rows": []}
        robustness_cache[key] = (time.time(), result)
        return result

    @app.post("/api/lab/backtest")
    async def lab_backtest(request: BacktestRequest):
        ex = hub["ex"]
        if request.timeframe not in LAB_TIMEFRAMES or not 30 <= request.days <= 2000:
            raise HTTPException(422, "Durée de bougie ou période non proposée.")
        market = (await ex.markets()).get(request.symbol)
        if not market or not market.get("spot"):
            raise HTTPException(404, f"{request.symbol} n'existe pas sur {exchange_id} en Spot.")
        if hub["backtesting"]:
            raise HTTPException(409, "Une comparaison est déjà en cours.")
        hub["backtesting"] = True
        try:
            candles = await fetch_history(ex, request.symbol, request.timeframe, request.days)
            if len(candles) < 400:
                raise HTTPException(422, f"Historique trop court : {len(candles)} bougies reçues, "
                                         "il en faut au moins 400. Allongez la période.")
            fee = ex.fees(request.symbol).get("taker") or settings.paper_fee_rate
            local = settings.model_copy(update={"paper_fee_rate": fee})
            tf_seconds = ex.timeframe_seconds(request.timeframe)
            rows = await asyncio.to_thread(compare_strategies, candles, local, request.symbol,
                                           tf_seconds, ex.limits(request.symbol))
        finally:
            hub["backtesting"] = False
        start = settings.paper_starting_cash
        return {
            "symbol": request.symbol, "timeframe": request.timeframe, "exchange": exchange_id,
            "candles": len(candles), "from_ms": candles[0][0], "to_ms": candles[-1][0],
            "fee_rate": fee, "buy_and_hold": candles[-1][4] / candles[0][4] - 1,
            "hold_curve": thin([c[4] / candles[0][4] - 1 for c in candles]),
            "times_ms": thin([c[0] for c in candles]),
            "rows": [{
                "name": r["strategy"], "label": r["strategy_label"], "return": r["return"],
                "first_half": r["first_half"], "second_half": r["second_half"],
                "max_drawdown": r["max_drawdown"], "trades": r["trades"],
                "win_rate": r["win_rate"], "profit_factor": r["profit_factor"],
                "fees": r["fees"], "curve": thin([v / start - 1 for v in r["equity_curve"]]),
            } for r in rows],
        }

    def registry_overview() -> dict:
        lab = hub["lab"]
        rows = {r["name"]: r for r in lab.standings(settings.strategy)}
        out = hub["registry"].overview()
        for row in out:
            row["paper"] = (paper_evidence(rows[row["name"]], lab.since) if row["name"] in rows else
                            {"passed": False, "reason": "stratégie réservée au backtest : pas d'essai papier"})
        return {"strategies": out, "basket": DEFAULT_BASKET}

    @app.get("/api/registry")
    async def registry():
        return registry_overview()

    @app.post("/api/lab/evaluate")
    async def lab_evaluate(request: EvaluateRequest):
        symbols = [s.strip().upper() for s in request.symbols if s.strip()]
        if (not symbols or len(symbols) > 8 or request.timeframe not in LAB_TIMEFRAMES
                or not 180 <= request.days <= 2000):
            raise HTTPException(422, "Panier, durée de bougie ou période non proposés.")
        if hub["backtesting"]:
            raise HTTPException(409, "Un calcul est déjà en cours.")
        hub["backtesting"] = True
        try:
            await evaluate_all(hub["ex"], settings, hub["registry"], symbols,
                               request.timeframe, request.days)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        finally:
            hub["backtesting"] = False
        return registry_overview()

    @app.post("/api/registry/promote")
    async def promote(request: PromoteRequest):
        reg, name = hub["registry"], request.strategy
        try:
            before = reg.status(name)
            if before == "candidate":
                evidence = reg.latest_evaluation(name)
            elif before == "backtest_ok":
                rows = {r["name"]: r for r in hub["lab"].standings(settings.strategy)}
                evidence = paper_evidence(rows[name], hub["lab"].since)
            else:
                evidence = None
            after = reg.promote(name, evidence, by="interface")
        except (PermissionError, ValueError, KeyError) as exc:
            raise HTTPException(409, f"Promotion refusée. {exc}")
        hub["coordinator"].bus.publish(Event(
            source="coordinator", target="coordinator", type="registry.change",
            correlation_id="manual",
            result=f"Stratégie {name} : {STATUS_LABELS[before]} → {STATUS_LABELS[after]}.",
            payload={"strategy": name, "from": before, "to": after}))
        return registry_overview()

    @app.get("/api/verify")
    async def verify():
        intact, count, bad_line = await asyncio.to_thread(verify_chain, settings.event_log_path)
        store = hub["coordinator"].bus.store
        database = (await asyncio.to_thread(store.compare_with_journal, Path(settings.event_log_path))
                    if store else None)
        return {"intact": intact, "count": count, "bad_line": bad_line, "database": database}

    @app.get("/api/overview")
    async def overview():
        """Synthèse pour les espaces Portefeuille, Risque, Plateformes et Configuration."""
        s = settings
        venues = []
        for v in hub["venues"]:
            p = v["broker"].portfolio
            exposure = sum(x["quantity"] * x["mark_price"] for x in p.positions.values())
            reserved = v["broker"].reserved()
            equity = p.equity or 0.0
            open_risk = sum(x["quantity"] * max(0.0, x["mark_price"] - x["stop_price"])
                            for x in p.positions.values())
            venues.append({
                "exchange": v["id"], "start": v["start"], "equity": equity, "cash": p.cash,
                "reserved": reserved, "positions": p.positions, "pending": p.pending,
                "watchlist": v["watchlist"], "fee_rate": v["coordinator"].settings.paper_fee_rate,
                "health": getattr(v["ex"], "health", None) and v["ex"].health.snapshot(),
                "risk": {
                    "exposure_pct": (exposure + reserved) / equity if equity else None,
                    "max_exposure_pct": s.max_total_exposure_pct,
                    "cash_reserve_pct": (p.cash - reserved) / equity if equity else None,
                    "min_cash_reserve_pct": s.min_cash_reserve_pct,
                    "daily_pnl": p.daily_pnl, "daily_limit": -equity * s.max_daily_loss_pct,
                    "weekly_pnl": p.weekly_pnl, "weekly_limit": -equity * s.max_weekly_loss_pct,
                    "slots": len(p.positions) + len(p.pending), "max_slots": s.max_open_positions,
                    "consecutive_losses": p.consecutive_losses,
                    "max_consecutive_losses": s.max_consecutive_losses,
                    "open_risk": open_risk, "risk_per_trade": equity * s.risk_per_trade_pct,
                    "max_correlated_risk": equity * s.max_correlated_risk_pct}})
        return {"venues": venues, "paused": hub["coordinator"].performance.pauses.all(),
                "halted": hub["coordinator"].bus.halted, "halt_reason": hub["coordinator"].bus.halt_reason,
                "recording_ok": hub["coordinator"].bus.recording_ok,
                "config": public_config(settings), "fingerprint": hub.get("config_fingerprint")}

    @app.get("/api/performance")
    async def performance():
        coordinator = hub["coordinator"]
        if not coordinator.performance.last:
            raise HTTPException(404, "Pas encore de mesure : elle est faite après chaque tour de cycles.")
        return {**coordinator.performance.last, "paused": coordinator.performance.pauses.all()}

    @app.post("/api/strategies/resume")
    async def resume_strategy(request: ResumeStrategyRequest):
        coordinator = hub["coordinator"]
        try:
            entry = coordinator.performance.pauses.resume(request.strategy, by="interface")
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        coordinator.bus.publish(Event(
            source="coordinator", target="strategy_analyst", type="registry.change",
            correlation_id="manual", priority="high",
            result=f"Pause de la stratégie {request.strategy} levée depuis l'interface "
                   f"(elle avait été mise en pause pour : {entry['reason']}).",
            payload={"strategy": request.strategy, "action": "resume", "by": "interface"}))
        return {"paused": coordinator.performance.pauses.all()}

    @app.get("/api/ai")
    async def ai_status():
        return hub["coordinator"].ai.client.usage()

    @app.post("/api/explain")
    async def explain_thread(request: ExplainRequest):
        from trading_agent.ai import AIUnavailable
        events = [e.model_dump(mode="json") for e in hub["history"] if e.correlation_id == request.thread]
        if not events:
            raise HTTPException(404, "Fil inconnu.")
        try:
            return await hub["coordinator"].ai.explain(request.thread, events)
        except AIUnavailable as exc:
            raise HTTPException(503, str(exc))

    holdings_cache: dict = {"at": 0.0, "key": None, "prices": {}, "now_ms": 0}

    @app.get("/api/holdings")
    async def holdings_view():
        """Vrai portefeuille, lecture seule : soldes saisis à la main, cours publics.
        Les cours sont gardés 60 s pour ne pas interroger les plateformes à chaque
        rafraîchissement de la page."""
        import time
        from trading_agent import holdings
        data = holdings.load(holdings.DEFAULT_PATH)
        key = sorted(data["lines"])
        if key and (holdings_cache["key"] != key or time.monotonic() - holdings_cache["at"] > 60):
            prices, now_ms = await holdings.fetch_prices(data)
            holdings_cache.update(at=time.monotonic(), key=key, prices=prices, now_ms=now_ms)
        now_ms = int(time.time() * 1000)
        return holdings.valuate(data, holdings_cache["prices"], now_ms)

    @app.get("/api/news")
    async def news_latest():
        return {"exchanges": hub["coordinator"].news.latest, "blocking": hub["coordinator"].news.blocking,
                "enabled": settings.news_enabled}

    @app.get("/api/calendar")
    async def calendar_latest():
        from trading_agent import macro_calendar
        news = hub["coordinator"].news
        if news.calendar is None:   # pas encore de cycle : lecture directe du fichier
            calendar = macro_calendar.load()
            calendar["upcoming"] = macro_calendar.upcoming(calendar) if not calendar["error"] else []
        else:
            calendar = news.calendar
        return {"upcoming": calendar.get("upcoming", []), "error": calendar.get("error"),
                "stale": calendar.get("stale", True), "covered_until": calendar.get("covered_until"),
                "updated": calendar.get("updated")}

    @app.get("/api/scenarios")
    async def scenarios_last():
        path = Path(settings.scenarios_path)
        if not path.exists():
            raise HTTPException(404, "Aucun test de scénarios extrêmes lancé pour l'instant.")
        import json
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post("/api/scenarios")
    async def scenarios_run():
        from trading_agent import scenarios
        report = await scenarios.run_all()     # dossiers temporaires isolés, aucun réseau
        scenarios.save(report, settings.scenarios_path)
        return report

    @app.get("/api/learning")
    async def learning_state():
        store = hub["coordinator"].learning.store
        return {"active": store.active(), "versions": store.versions()[:30],
                "busy": hub["coordinator"].learning.busy}

    @app.post("/api/learning/propose")
    async def learning_propose(request: LearningRequest):
        from trading_agent.evaluate import DEFAULT_BASKET
        from trading_agent.evaluate_cli import load_histories
        from trading_agent.learning import propose
        learning = hub["coordinator"].learning
        if request.strategy not in STRATEGIES:
            raise HTTPException(404, "Stratégie inconnue.")
        if learning.busy:
            raise HTTPException(409, "Une recherche de réglage est déjà en cours.")
        learning.busy = True
        try:
            histories = await load_histories(hub["ex"], DEFAULT_BASKET, "4h", 730)
            proposal = await asyncio.to_thread(propose, request.strategy, histories, settings,
                                               hub["ex"].timeframe_seconds("4h"),
                                               learning.store.params(request.strategy),
                                               learning.store.trials(request.strategy))
            learning.publish_proposal(proposal, "learn-" + uuid.uuid4().hex[:8])
            return proposal
        finally:
            learning.busy = False

    @app.post("/api/learning/apply")
    async def learning_apply(request: LearningRequest):
        return _learning_change("apply", request)

    @app.post("/api/learning/rollback")
    async def learning_rollback(request: LearningRequest):
        return _learning_change("rollback", request)

    def _learning_change(action: str, request: LearningRequest) -> dict:
        store = hub["coordinator"].learning.store
        try:
            entry = (store.apply(request.id or "", by="interface") if action == "apply"
                     else store.rollback(request.strategy or "", by="interface"))
        except (PermissionError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        name = request.strategy or next((v["strategy"] for v in store.versions() if v["id"] == request.id), "?")
        hub["coordinator"].bus.publish(Event(
            source="coordinator", target="strategy_analyst", type="registry.change", correlation_id="manual",
            priority="high", payload={"strategy": name, "action": f"learning_{action}", "by": "interface",
                                      "version": entry.get("version"), "params": entry.get("params")},
            result=(f"Réglage {entry.get('version')} appliqué à {name} depuis l'interface : {entry.get('params')}."
                    if action == "apply" else
                    f"Retour au réglage précédent pour {name} depuis l'interface : {entry.get('params') or 'origine'}.")))
        return {"active": store.active()}

    @app.get("/api/db")
    async def db_status():
        store = hub["coordinator"].bus.store
        if not store:
            raise HTTPException(404, "Aucune base de données configurée.")
        return await asyncio.to_thread(store.stats)

    @app.get("/api/search")
    async def search(thread: str | None = None, type: str | None = None, source: str | None = None,
                     symbol: str | None = None, status: str | None = None, since: str | None = None,
                     until: str | None = None, text: str | None = None, limit: int = 200):
        store = hub["coordinator"].bus.store
        if not store:
            raise HTTPException(404, "Aucune base de données configurée.")
        return await asyncio.to_thread(store.search, thread, type, source, symbol, status,
                                       since, until, text, limit)

    @app.get("/api/stream")
    async def stream():
        queue: asyncio.Queue = asyncio.Queue()
        hub["clients"].add(queue)

        async def generate():
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), 15)
                        yield f"data: {event.model_dump_json()}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                hub["clients"].discard(queue)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.post("/api/cycle")
    async def run_cycle_now():
        if hub["coordinator"].bus.halted:
            raise HTTPException(409, "Système à l'arrêt : reprenez avant de lancer un cycle.")
        if hub["running"]:
            raise HTTPException(409, "Un cycle est déjà en cours.")
        hub["trigger"].set()
        return {"ok": True}

    @app.post("/api/halt")
    async def halt(request: HaltRequest):
        reason = request.reason or "demandé depuis l'interface"
        hub["coordinator"].halt(reason + (" ; fermeture de toutes les positions demandée" if request.flatten else ""))
        closed = []
        if request.flatten:
            for venue in hub["venues"]:
                closed += [r for r in venue["coordinator"].execution.flatten("manual")
                           if r.get("status") == "filled"]
        return {"halted": True, "closed": len(closed)}

    @app.post("/api/resume")
    async def resume():
        bus = hub["coordinator"].bus
        if bus.halted:
            bus.publish(Event(source="coordinator", target="coordinator", type="system.resume",
                              correlation_id="manual",
                              result=f"Reprise depuis l'interface après : {bus.halt_reason}"))
        return {"halted": False}

    return app


def serve(exchange_id: str, symbol: str, timeframe: str, interval: float | None, port: int,
          strategy: str | None = None, also: list[str] | None = None) -> None:
    import uvicorn
    settings = Settings()
    if strategy:
        settings.strategy = strategy
    if also:
        settings.extra_exchanges = list(also)
    print(f"Interface Jarvis : http://127.0.0.1:{port}  (Ctrl+C pour arrêter)")
    uvicorn.run(create_app(exchange_id, symbol, timeframe, interval, settings),
                host="127.0.0.1", port=port, log_level="warning",
                timeout_graceful_shutdown=3)
