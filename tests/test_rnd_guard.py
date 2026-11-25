"""Tests du garde-fou R&D : à copier dans tests/ de Jarvis."""
import json
import subprocess
from pathlib import Path

from rnd.guard import verifier

CFG = json.loads((Path(__file__).resolve().parents[1] / "rnd" / "config.json").read_text(encoding="utf-8"))


def _repo(tmp_path):
    g = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "s.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_s.py").write_text("def test_a():\n    assert True\n")
    g("add", "-A"); g("commit", "-qm", "init"); g("checkout", "-qb", "rnd/x")
    return g


def _commit(tmp_path, g, fichier, contenu):
    p = tmp_path / fichier; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(contenu)
    g("add", "-A"); g("commit", "-qm", "c")


def test_changement_sain_accepte(tmp_path):
    g = _repo(tmp_path)
    _commit(tmp_path, g, "strategies/s.py", "x = 2\n")
    assert verifier(tmp_path, "main", "rnd/x", CFG)["ok"]


def test_chemin_risque_refuse(tmp_path):
    g = _repo(tmp_path)
    _commit(tmp_path, g, "trading_agent/risk_engine.py", "MAX = 1\n")
    r = verifier(tmp_path, "main", "rnd/x", CFG)
    assert not r["ok"] and any("protégé" in v for v in r["violations"])


def test_ordre_reel_refuse(tmp_path):
    g = _repo(tmp_path)
    _commit(tmp_path, g, "strategies/s.py", "ex.create_order('BTC/EUR', 'market', 'buy', 1)\n")
    assert not verifier(tmp_path, "main", "rnd/x", CFG)["ok"]


def test_secret_refuse(tmp_path):
    g = _repo(tmp_path)
    _commit(tmp_path, g, "strategies/s.py", "api_key = 'abc'\n")
    assert not verifier(tmp_path, "main", "rnd/x", CFG)["ok"]


def test_import_reference_refuse(tmp_path):
    g = _repo(tmp_path)
    _commit(tmp_path, g, "strategies/s.py", "from reference_code.x import y\n")
    assert not verifier(tmp_path, "main", "rnd/x", CFG)["ok"]


def test_suppression_de_test_refusee(tmp_path):
    g = _repo(tmp_path)
    _commit(tmp_path, g, "tests/test_s.py", "")
    assert not verifier(tmp_path, "main", "rnd/x", CFG)["ok"]


def test_skip_refuse(tmp_path):
    g = _repo(tmp_path)
    _commit(tmp_path, g, "tests/test_s.py", "import pytest\n@pytest.mark.skip\ndef test_a():\n    assert True\n")
    assert not verifier(tmp_path, "main", "rnd/x", CFG)["ok"]


def test_dependance_refusee(tmp_path):
    g = _repo(tmp_path)
    _commit(tmp_path, g, "requirements.txt", "requests\n")
    assert not verifier(tmp_path, "main", "rnd/x", CFG)["ok"]
