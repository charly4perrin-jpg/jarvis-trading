"""Rapport de performance exposé par le serveur : lecture seule, erreur affichée plutôt
qu'un chiffre deviné."""
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from tests.test_agents import BUY
from tests.test_server import FakeServerExchange
from trading_agent.config import Settings
from trading_agent.server import create_app
from trading_agent.strategies import LIVE_STRATEGIES as STRATEGIES


def client(tmp_path):
    settings = Settings(
        _env_file=None,
        event_log_path=str(tmp_path / "events.jsonl"), halt_flag_path=str(tmp_path / "HALT"),
        paper_ledger_path=str(tmp_path / "ledger.jsonl"), paper_state_path=str(tmp_path / "state.json"),
        lab_dir=str(tmp_path / "lab"), lab_daily_dir=str(tmp_path / "lab_1d"),
        shadow_dir=str(tmp_path / "shadow"), notifications_path=str(tmp_path / "n.jsonl"),
        notify_macos=False, regime_filter=False, registry_path=str(tmp_path / "registry.json"),
        evaluations_dir=str(tmp_path / "evaluations"), database_path=str(tmp_path / "jarvis.db"),
        pause_path=str(tmp_path / "paused.json"), performance_dir=str(tmp_path / "perf"),
        arbitrage_log_path=str(tmp_path / "arb.jsonl"), news_enabled=False)
    app = create_app("fake", "BTC/EUR", "1m", settings=settings,
                     exchange_factory=lambda _id: FakeServerExchange(BUY), auto_run=False)
    return TestClient(app)


def test_tearsheet_lists_every_strategy_and_reports_missing_shadow_as_error(tmp_path):
    with client(tmp_path) as c:
        for tf in ("principal", "1d"):
            r = c.get(f"/api/tearsheet?tf={tf}")
            assert r.status_code == 200
            d = r.json()
            assert set(d["lab"]) == set(STRATEGIES)
            assert all("error" in x or x["metrics"]["return"] == 0 for x in d["lab"].values())
            assert "error" in d["shadow"]["fake"]           # pas de fichier : refusé, pas inventé


def test_tearsheet_reads_shadow_trades_without_writing(tmp_path):
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    closed_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    path = shadow / "fake.json"
    path.write_text(json.dumps({"closed": [{"closed_at": closed_at, "r": 2.0, "blocked_by": "risk"}]}))
    before = path.read_bytes()
    with client(tmp_path) as c:
        report = c.get("/api/tearsheet").json()["shadow"]["fake"]
    assert report["trades"]["trades"] == 1 and report["metrics"]["return"] > 0
    assert path.read_bytes() == before


def test_broken_shadow_file_is_an_error_not_a_crash(tmp_path):
    (tmp_path / "shadow").mkdir()
    (tmp_path / "shadow" / "fake.json").write_text("{abîmé")
    with client(tmp_path) as c:
        r = c.get("/api/tearsheet")
    assert r.status_code == 200 and "error" in r.json()["shadow"]["fake"]


def test_robustness_is_inconclusive_without_trades_then_computed(tmp_path):
    with client(tmp_path) as c:
        d = c.get("/api/robustness").json()
        assert d["error"] is None and {r["name"] for r in d["rows"]} == set(STRATEGIES)
        assert all(r["status"] == "insuffisant" for r in d["rows"])
    sells = [{"side": "sell", "realized_pnl": p, "fee": 1.0, "entry_fee": 1.0, "quantity": 1.0,
              "entry_price": 1000.0, "fill_price": 1000.0 + p} for p in [8.0, -5.0] * 6]
    (tmp_path / "lab" / "trend_ledger.jsonl").write_text("\n".join(json.dumps(s) for s in sells))
    with client(tmp_path) as c:
        row = next(r for r in c.get("/api/robustness").json()["rows"] if r["name"] == "trend")
    assert row["status"] == "ok" and row["trades"] == 12
    assert any("moins de 30" in w for w in row["warnings"])
