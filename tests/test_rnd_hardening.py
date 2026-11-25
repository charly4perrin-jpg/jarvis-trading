"""Le garde-fou R&D face aux contournements repérés à l'installation (17-18/11/2026)."""
import json
import subprocess
from pathlib import Path

import pytest

from rnd.guard import verifier

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "rnd" / "config.json").read_text(encoding="utf-8"))


def repo(tmp_path):
    g = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (tmp_path / "trading_agent").mkdir()
    (tmp_path / "trading_agent" / "strategies.py").write_text("x = 1\n")
    g("add", "-A")
    g("commit", "-qm", "init")
    g("checkout", "-qb", "rnd/x")
    return g


def essai(tmp_path, fichier, contenu):
    g = repo(tmp_path)
    p = tmp_path / fichier
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(contenu)
    g("add", "-A")
    g("commit", "-qm", "c")
    return verifier(tmp_path, "main", "rnd/x", CFG)


SENSIBLES = ["trading_agent/risk.py", "trading_agent/agents.py", "trading_agent/events.py", "trading_agent/store.py",
             "trading_agent/adversarial.py", "trading_agent/resilience.py", "trading_agent/config.py",
             "trading_agent/server.py", "trading_agent/ai.py", "trading_agent/notifications.py",
             "trading_agent/exchanges.py", "trading_agent/scenarios.py", "tests/conftest.py",
             "tests/test_risk_properties.py", "tests/test_safety_properties.py", "tests/test_protections.py",
             "rnd/guard.py", "rnd/config.json", ".claude/agents/validateur.md", "pyproject.toml", ".env",
             "data/paper_state.json", "launchd/com.jarvis.rnd.plist"]


@pytest.mark.parametrize("fichier", SENSIBLES)
def test_every_real_safety_file_is_refused(tmp_path, fichier):
    assert (ROOT / fichier).exists() or fichier in (".env", "data/paper_state.json"), fichier
    r = essai(tmp_path, fichier, "modifié = True\n")
    assert not r["ok"] and any("protégé" in v for v in r["violations"]), fichier


@pytest.mark.parametrize("fichier,contenu,raison", [
    ("trading_agent/conftest.py", "x = 1\n", "conftest"),
    ("sitecustomize.py", "x = 1\n", "démarrage"),
    ("trading_agent/patch.pth", "import x\n", ".pth"),
    ("trading_agent/fuite.py", "import urllib.request\n", "réseau"),
    ("trading_agent/fuite.py", "data = open('.env').read()\n", ".env"),
    ("trading_agent/fuite.py", "key = os.environ.get('X')\n", "environnement"),
    ("trading_agent/lancer.py", "import subprocess\n", "programme"),
    ("trading_agent/patch.py", "setattr(risk, 'evaluate', lambda *a: ok)\n", "dynamique"),
    ("trading_agent/patch.py", "RiskManager.evaluate = laxiste\n", "sécurité"),
    ("trading_agent/patch.py", "import importlib\n", "dynamique"),
    ("trading_agent/lire.py", "x = Path('data/paper_state.json').read_text()\n", "données de l'essai"),
])
def test_bypass_routes_are_refused(tmp_path, fichier, contenu, raison):
    r = essai(tmp_path, fichier, contenu)
    assert not r["ok"] and any(raison in v for v in r["violations"]), r["violations"]


def test_network_is_still_allowed_inside_tests(tmp_path):
    assert essai(tmp_path, "tests/test_x.py", "import urllib\ndef test_a():\n    assert True\n")["ok"]


def test_an_ordinary_strategy_change_passes(tmp_path):
    r = essai(tmp_path, "trading_agent/strategies.py", "x = 2\n\ndef nouvelle(c):\n    return None\n")
    assert r["ok"], r["violations"]


def test_loop_never_links_the_python_environment_and_does_not_merge_alone():
    from rnd import loop
    assert ".venv" not in loop.LIENS
    assert loop.PY.startswith("/") and loop.PY.endswith("/.venv/bin/python")
    assert CFG["fusion_automatique"] is False
    for interdit in ("WebFetch", "WebSearch", "Read(**/.env)"):
        assert interdit in CFG["outils_interdits"]
