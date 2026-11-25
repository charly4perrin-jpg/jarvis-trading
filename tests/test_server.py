import time

from fastapi.testclient import TestClient

from tests.test_agents import BUY, FakeExchange
from trading_agent.config import Settings
from trading_agent.server import create_app
from trading_agent.strategies import LIVE_STRATEGIES, STRATEGIES


class FakeServerExchange(FakeExchange):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def markets(self):
        return {"BTC/EUR": {"spot": True, "active": True}}

    def fees(self, symbol):
        return {"taker": 0.0025, "maker": 0.0015}

    async def ohlcv(self, symbol, timeframe="1m", limit=100, since=None):
        return await super().ohlcv(symbol, timeframe, limit)


def make_client(tmp_path, closes=None):
    settings = Settings(
        _env_file=None,
        event_log_path=str(tmp_path / "events.jsonl"), halt_flag_path=str(tmp_path / "HALT"),
        paper_ledger_path=str(tmp_path / "ledger.jsonl"), paper_state_path=str(tmp_path / "state.json"),
        lab_dir=str(tmp_path / "lab"), notifications_path=str(tmp_path / "notifications.jsonl"),
        notify_macos=False, regime_filter=False, registry_path=str(tmp_path / "registry.json"),
        evaluations_dir=str(tmp_path / "evaluations"), database_path=str(tmp_path / "jarvis.db"),
        pause_path=str(tmp_path / "paused.json"), performance_dir=str(tmp_path / "perf"),
        limit_min_saving_bps=1000, arbitrage_log_path=str(tmp_path / "arb.jsonl"),
        news_enabled=False)
    app = create_app("fake", "BTC/EUR", "1m", settings=settings,
                     exchange_factory=lambda _id: FakeServerExchange(closes or BUY), auto_run=False)
    return TestClient(app)


def wait_for(client, event_type):
    for _ in range(100):
        events = client.get("/api/events").json()
        if any(e["type"] == event_type for e in events):
            return events
        time.sleep(0.02)
    raise AssertionError(f"{event_type} jamais reçu")


def test_page_and_state_are_served(tmp_path):
    with make_client(tmp_path) as client:
        assert "Jarvis" in client.get("/").text
        state = client.get("/api/state").json()
        assert len(state["agents"]) == 19
        assert {a["label"] for a in state["agents"]} >= {"Risque", "Exécution", "Audit"}
        assert state["system"]["mode"] == "paper" and not state["system"]["halted"]
        # Au démarrage, un seul vrai message : la version de la configuration ; rien d'inventé.
        counts = {(l["source"], l["target"]): l["count"] for l in state["links"]}
        assert counts.pop(("coordinator", "auditor")) == 1 and set(counts.values()) == {0}
        assert [e["type"] for e in client.get("/api/events").json()] == ["config.loaded"]


def test_cycle_on_demand_feeds_events_and_links(tmp_path):
    with make_client(tmp_path) as client:
        assert client.post("/api/cycle").status_code == 200
        events = wait_for(client, "audit.thread_recorded")
        assert "execution.fill" in [e["type"] for e in events]
        state = client.get("/api/state").json()
        used = {(l["source"], l["target"]) for l in state["links"] if l["count"]}
        assert ("risk_manager", "execution") in used
        assert "BTC/EUR" in state["portfolio"]["positions"]


def test_halt_blocks_cycles_until_resume(tmp_path):
    with make_client(tmp_path) as client:
        assert client.post("/api/halt", json={"reason": "test"}).json() == {"halted": True, "closed": 0}
        assert client.get("/api/state").json()["system"]["halted"]
        assert client.post("/api/cycle").status_code == 409
        client.post("/api/resume")
        assert not client.get("/api/state").json()["system"]["halted"]
        assert client.post("/api/cycle").status_code == 200
        wait_for(client, "cycle.end")


def test_second_server_on_same_data_is_refused(tmp_path):
    import os
    import pytest
    with make_client(tmp_path):
        pass  # arrêt propre : le verrou est retiré
    assert not (tmp_path / "server.lock").exists()
    (tmp_path / "server.lock").write_text(str(os.getppid()))  # un autre processus bien vivant
    with pytest.raises(RuntimeError, match="tourne déjà"):
        with make_client(tmp_path):
            pass


def test_verify_reports_journal_integrity(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/cycle")
        wait_for(client, "audit.thread_recorded")
        report = client.get("/api/verify").json()
        assert report["intact"] and report["count"] >= 10
        log = tmp_path / "events.jsonl"
        line = next(i for i, text in enumerate(log.read_text().splitlines(), 1) if "Nouveau cycle" in text)
        log.write_text(log.read_text().replace("Nouveau cycle", "Cycle falsifié", 1))
        report = client.get("/api/verify").json()
        assert not report["intact"] and report["bad_line"] == line


def test_lab_competition_gives_each_strategy_its_own_portfolio(tmp_path):
    with make_client(tmp_path) as client:
        before = client.get("/api/lab").json()
        assert len(before["standings"]) == len(LIVE_STRATEGIES) and all(r["equity"] == 3000 for r in before["standings"])
        client.post("/api/cycle")
        events = wait_for(client, "audit.thread_recorded")
        lab = client.get("/api/lab").json()
        by_name = {r["name"]: r for r in lab["standings"]}
        assert by_name["momentum_demo"]["in_position"]       # le signal de démo a été joué
        assert not by_name["trend"]["in_position"]           # pas assez d'historique pour les autres
        assert sum(r["active"] for r in lab["standings"]) == 1 and lab["error"] is None
        notes = [e for e in events if e["type"] == "strategy.lab"]
        assert len(notes) == 1 and "portefeuille fictif séparé" in notes[0]["result"]
        # Le portefeuille principal reste distinct de ceux du laboratoire.
        assert (tmp_path / "lab" / "momentum_demo_state.json").exists()


def test_lab_backtest_compares_all_strategies(tmp_path):
    import math
    closes = [100 + 15 * math.sin(i / 12) + i * 0.02 for i in range(700)]
    with make_client(tmp_path, closes) as client:
        r = client.post("/api/lab/backtest", json={"symbol": "BTC/EUR", "timeframe": "1h", "days": 365})
        assert r.status_code == 200
        data = r.json()
        assert [row["name"] for row in data["rows"]] == [n for n, s in STRATEGIES.items() if s.universe is None]
        assert data["candles"] == 700 and len(data["hold_curve"]) == 240 == len(data["rows"][0]["curve"])
        assert client.post("/api/lab/backtest", json={"symbol": "XXX/EUR"}).status_code == 404
        assert client.post("/api/lab/backtest", json={"symbol": "BTC/EUR", "timeframe": "3m"}).status_code == 422


def test_notifications_are_listed_newest_first(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/api/notifications").json() == []
        client.post("/api/cycle")
        wait_for(client, "audit.thread_recorded")
        client.post("/api/halt", json={"reason": "test"})
        items = client.get("/api/notifications").json()
        assert items[0]["title"] == "Système arrêté"
        assert {"Achat simulé", "Laboratoire"} <= {i["title"] for i in items}


def test_registry_starts_as_candidates_and_refuses_unproven_promotion(tmp_path):
    with make_client(tmp_path) as client:
        data = client.get("/api/registry").json()
        assert [s["status"] for s in data["strategies"]] == ["candidate"] * len(STRATEGIES)
        assert all(s["evaluation"] is None and not s["paper"]["passed"] for s in data["strategies"])
        refused = client.post("/api/registry/promote", json={"strategy": "breakout"})
        assert refused.status_code == 409 and "Aucune évaluation" in refused.json()["detail"]
        assert client.get("/api/registry").json()["strategies"][1]["status"] == "candidate"


def test_database_search_and_status(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/cycle")
        wait_for(client, "audit.thread_recorded")
        status = client.get("/api/db").json()
        assert status["events"] >= 5 and status["ok"]
        found = client.get("/api/search", params={"type": "market.snapshot"}).json()
        assert found and all(e["type"] == "market.snapshot" for e in found)
        thread = client.get("/api/search", params={"thread": found[0]["correlation_id"]}).json()
        assert any(e["type"] == "cycle.end" for e in thread)
        assert client.get("/api/verify").json()["database"]["consistent"]


def test_overview_and_config_versioning(tmp_path):
    with make_client(tmp_path) as client:
        first = next(e for e in client.get("/api/events").json() if e["type"] == "config.loaded")
        overview = client.get("/api/overview").json()
        assert overview["fingerprint"] == first["payload"]["fingerprint"]
        assert overview["config"]["max_daily_loss_pct"] == 0.02
        assert not any("key" in k or "secret" in k for k in overview["config"])
        venue = overview["venues"][0]
        assert venue["exchange"] == "fake" and venue["risk"]["max_slots"] == 5
    with make_client(tmp_path) as client:          # redémarrage, même configuration
        loaded = [e for e in client.get("/api/events").json() if e["type"] == "config.loaded"]
        assert "identique" in loaded[-1]["result"]
    from trading_agent.server import config_fingerprint, public_config
    from trading_agent.config import Settings
    assert config_fingerprint(public_config(Settings(_env_file=None, max_daily_loss_pct=0.01))) != \
        config_fingerprint(public_config(Settings(_env_file=None)))


def test_chart_shows_candles_and_jarvis_trades(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/cycle")
        wait_for(client, "execution.fill")
        d = client.get("/api/chart", params={"exchange": "fake", "symbol": "BTC/EUR"}).json()
        assert d["candles"] and d["candles"][-1]["close"] == 130.0
        buy = d["markers"][0]
        assert buy["side"] == "buy" and buy["time"] % 60 == 0
        assert any(c["time"] == buy["time"] for c in d["candles"]) or buy["time"] >= d["candles"][-1]["time"]
        assert d["position"]["stop"] < d["position"]["entry"]
        assert client.get("/api/chart", params={"exchange": "inconnue", "symbol": "BTC/EUR"}).status_code == 404
        js = client.get("/vendor/lightweight-charts.js")
        assert js.status_code == 200 and "createChart" in js.text
        assert client.get("/vendor/..%2F..%2Fconfig.py").status_code == 404
