"""La démo publique ne doit jamais laisser sortir de donnée sensible."""
import json

from demo import export_snapshot as ex


def _ecrire(tmp_path, evenements):
    d = tmp_path / "data"; d.mkdir()
    with open(d / "events.jsonl", "w", encoding="utf-8") as f:
        for e in evenements:
            f.write(json.dumps(e) + "\n")
    return d


def test_masquage_et_liste_blanche(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "DATA", _ecrire(tmp_path, [
        {"ts": "2099-01-01T00:00:00", "type": "cycle.end", "source": "coordinator", "target": "auditor", "status": "ok",
         "result": "contact moi@exemple.fr /Users/x/secret api_key=abc123 xai-ABCDEFGHIJKLMNOP 192.168.1.4", "hash": "f" * 64},
        {"ts": "2099-01-01T00:00:01", "type": "config.loaded", "source": "coordinator", "status": "ok",
         "result": "password: tropsecret", "payload": {"OPENAI_API_KEY": "sk-zzz"}},
        {"ts": "2099-01-01T00:00:02", "type": "ai.explanation", "source": "coordinator", "status": "ok", "result": "avis IA"},
    ]))
    s = json.dumps(ex.construire(), ensure_ascii=False)
    for interdit in ("moi@exemple.fr", "/Users/x", "abc123", "xai-ABCD", "192.168.1.4", "tropsecret", "sk-zzz", "avis IA", "config.loaded"):
        assert interdit not in s
    assert "Argent fictif".lower() in s.lower()


def test_valeurs_non_finies_neutralisees():
    assert ex.nombre(float("nan")) is None and ex.nombre(float("inf")) is None and ex.nombre(1.23456) == 1.2346


def test_sans_donnees(tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "DATA", tmp_path / "absent")
    s = ex.construire()
    assert s["fil"] == [] and s["portefeuilles"] == []


def test_page_rejouee_conforme_a_la_demo(tmp_path, capsys):
    """Les chiffres affichés par la page publique sont ceux que produit la vraie démo."""
    from pathlib import Path
    from trading_agent.demo_run import main
    main(str(tmp_path / "demo"))
    sortie = capsys.readouterr().out
    page = (Path(__file__).resolve().parents[1] / "demo/site/index.html").read_text(encoding="utf-8")
    for chiffre in ("130,05", "91,91", "18,25", "100,05", "104,95", "5,61", "2 987,36", "12,64"):
        assert chiffre in sortie and chiffre in page, chiffre
    assert "Aucun ordre réel" in page and "pas un conseil en investissement" in page
