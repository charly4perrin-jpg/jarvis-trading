"""Démo rejouable (python -m trading_agent demo) : les quatre vérifications."""
import asyncio
import socket
import subprocess
from pathlib import Path

import pytest

from trading_agent import demo_run
from trading_agent.demo_run import REPO, check_isolation, run_demo


def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("la démo a tenté un accès réseau ou un processus externe")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def _check_story(r):
    refus, achat, stop, gain = r["steps"]
    assert refus["signal"] and refus["approved"] is False
    assert "carnet d'ordres périmé" in refus["reason"] and refus["cash_after"] == refus["cash_before"] == 3000
    assert achat["approved"] and achat["position_open"] and achat["fill_price"] == 130.05
    assert achat["quantity"] > 0 and achat["fee"] > 0
    assert stop["reason"] == "stop" and not stop["position_open"] and stop["fill_price"] < 99
    # Le cash final se retrouve à partir des montants de la trace.
    spent = achat["fill_price"] * achat["quantity"] + achat["fee"]
    received = stop["fill_price"] * stop["quantity"] - stop["fee"]
    assert stop["cash_after"] == pytest.approx(3000 - spent + received)
    # Trade gagnant : vendu par la règle de sortie, avec un bénéfice net des frais.
    assert gain["approved"] and gain["reason"] == "exit_signal" and gain["realized_pnl"] > 0
    assert gain["fill_price"] > gain["buy_price"] and not gain["position_open"]
    spent2 = gain["buy_price"] * gain["quantity"] + gain["buy_fee"]
    received2 = gain["fill_price"] * gain["exit_quantity"] - gain["fee"]
    assert r["final_cash"] == pytest.approx(stop["cash_after"] - spent2 + received2)
    assert not r["halted"]


def test_sans_reseau_ni_secret(tmp_path, monkeypatch):
    """1. Marche sans .env et sans réseau, même si un .env piégé traîne dans le dossier."""
    (tmp_path / ".env").write_text("APP_ENV=live\nXAI_API_KEY=piege\nOPENAI_API_KEY=piege\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_ENV", "live")
    _no_network(monkeypatch)
    s = demo_run.demo_settings(tmp_path / "demo")
    assert s.app_env == "paper" and s.xai_api_key is None and s.openai_api_key is None
    assert not s.news_enabled and not s.notify_macos and s.notify_email_to is None
    _check_story(asyncio.run(run_demo(tmp_path / "demo")))


@pytest.mark.parametrize("bad", ["data", "data/lab/x", "data/shadow"])
def test_refuse_les_chemins_de_l_essai_principal(bad):
    """2. Refus de démarrer si le dossier touche la base ou les journaux de l'essai."""
    before = sorted(p.name for p in (REPO / "data").iterdir()) if (REPO / "data").exists() else []
    with pytest.raises(ValueError, match="essai principal"):
        asyncio.run(run_demo(REPO / bad))
    after = sorted(p.name for p in (REPO / "data").iterdir()) if (REPO / "data").exists() else []
    assert before == after


def test_refuse_un_fichier_de_l_essai(tmp_path):
    for name in ("jarvis.db", "events.jsonl", "paper_state.json", "paper_ledger.jsonl", "HALT"):
        with pytest.raises(ValueError, match="essai principal"):
            check_isolation(tmp_path, [str(REPO / "data" / name)])


def test_refuse_un_dossier_etranger_non_vide(tmp_path):
    (tmp_path / "important.txt").write_text("à garder")
    with pytest.raises(ValueError, match="pas un dossier de démo"):
        asyncio.run(run_demo(tmp_path))
    assert (tmp_path / "important.txt").exists()


def _strip(r):
    return [{k: v for k, v in s.items() if not k.endswith("thread")} for s in r["steps"]] + [r["final_cash"]]


def test_repetable(tmp_path, monkeypatch):
    """3. Deux lancements depuis le même état : mêmes décisions, prix, quantités, frais, soldes."""
    _no_network(monkeypatch)
    first = asyncio.run(run_demo(tmp_path / "demo"))
    second = asyncio.run(run_demo(tmp_path / "demo"))   # le dossier de démo est remis à zéro
    assert _strip(first) == _strip(second)
    assert [s["thread"] for s in first["steps"]] != [s["thread"] for s in second["steps"]]


def test_notice():
    """4. La notice donne la commande, les résultats attendus, le motif et le cash final."""
    text = (REPO / "docs" / "demo.md").read_text(encoding="utf-8")
    for needle in ("python -m trading_agent demo", "trace --log", "carnet d'ordres périmé",
                   "2 981,75", "Trade gagnant", "Démo : données préparées, argent fictif"):
        assert needle in text


def test_capital_fictif_choisi(tmp_path):
    r = asyncio.run(run_demo(tmp_path / "demo", cash=9_000))
    achat = r["steps"][1]
    assert r["start_cash"] == 9_000 and achat["position_open"]
    # Le risque reste 0,5 % du capital : la taille triple quand le capital triple.
    assert achat["quantity"] == pytest.approx(3 * 0.47167471, rel=1e-6)
    for bad in (0, -5, float("nan"), 1e9):
        with pytest.raises(ValueError, match="capital fictif"):
            asyncio.run(run_demo(tmp_path / "x", cash=bad))
