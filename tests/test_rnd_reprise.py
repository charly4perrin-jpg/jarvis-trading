"""R&D : un essai rejeté (après le garde-fou) est réappliqué au cycle suivant pour être
corrigé, au lieu d'être réécrit de zéro."""
import json
import subprocess

from rnd import loop

MISSION = {"titre": "Mission d'essai"}


def repo(tmp_path):
    d = tmp_path / "wt"
    d.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    (d / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                   cwd=d, check=True)
    return d


def save(tmp_path, monkeypatch, patch: str | None, raisons="défaut X"):
    monkeypatch.setattr(loop, "ESSAIS", tmp_path / "essais")
    (tmp_path / "essais").mkdir()
    cle = loop._cle(MISSION)
    if patch is not None:
        (tmp_path / "essais" / f"{cle}.patch").write_text(patch)
    (tmp_path / "essais" / f"{cle}.json").write_text(json.dumps({"raisons": raisons}))


PATCH = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-x = 1
+x = 2
"""


def test_no_previous_attempt_gives_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "ESSAIS", tmp_path / "essais")
    assert loop.essai_precedent(MISSION, repo(tmp_path)) == ""


def test_previous_code_is_reapplied_with_the_reasons(tmp_path, monkeypatch):
    d = repo(tmp_path)
    save(tmp_path, monkeypatch, PATCH)
    text = loop.essai_precedent(MISSION, d)
    assert "défaut X" in text and "déjà présent" in text
    assert (d / "a.py").read_text() == "x = 2\n"


def test_patch_that_no_longer_applies_keeps_only_the_reasons(tmp_path, monkeypatch):
    d = repo(tmp_path)
    (d / "a.py").write_text("y = 5\n")
    save(tmp_path, monkeypatch, PATCH)
    text = loop.essai_precedent(MISSION, d)
    assert "n'a pas pu être réappliqué" in text and (d / "a.py").read_text() == "y = 5\n"


def test_reasons_alone_without_code(tmp_path, monkeypatch):
    save(tmp_path, monkeypatch, None)
    assert "évite ces défauts" in loop.essai_precedent(MISSION, repo(tmp_path))


def test_accepted_attempt_is_forgotten(tmp_path, monkeypatch):
    save(tmp_path, monkeypatch, PATCH)
    loop.oublier_essai(MISSION)
    assert not list((tmp_path / "essais").iterdir())


def test_imposed_mission_reuses_existing_detail_without_duplicate(tmp_path, monkeypatch):
    store = [{"titre": "M", "detail": "exigences du validateur", "statut": "rejetee"}]
    monkeypatch.setattr(loop, "missions", lambda: store)
    monkeypatch.setattr(loop, "sauver_missions", lambda m: None)
    got = loop.prochaine_mission("M")
    assert got["detail"] == "exigences du validateur" and got["statut"] == "en_cours" and len(store) == 1
    assert loop.prochaine_mission("Nouvelle")["detail"] == "Nouvelle" and len(store) == 2


def test_obsidian_note_lists_missions_and_reports_only_in_its_own_file(tmp_path, monkeypatch):
    vault = tmp_path / "Jarvis"
    vault.mkdir()
    (vault / "Accueil.md").write_text("à moi")
    etat = tmp_path / "etat"
    (etat / "rapports").mkdir(parents=True)
    (etat / "rapports" / "1.json").write_text(json.dumps(
        {"debut": "2026-11-19 14:00:00", "mission": {"titre": "M"}, "verdict": "rejetee",
         "erreur": "défaut | avec\nretour"}))
    monkeypatch.setattr(loop, "ETAT", etat)
    monkeypatch.setattr(loop, "missions", lambda: [{"titre": "M", "statut": "fait"}])
    note = loop.exporter_obsidian(str(vault)).read_text()
    assert "| M | fait |" in note and "rejetée" in note and "défaut   avec retour" in note
    assert (vault / "Accueil.md").read_text() == "à moi"
    assert loop.exporter_obsidian(str(tmp_path / "absent")) is None
