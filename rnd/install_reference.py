"""Clone les dépôts ★ de la bibliothèque dans reference_code/ (copies de lecture, sans .git).

  .venv/bin/python -m rnd.install_reference              # les 160 ★
  .venv/bin/python -m rnd.install_reference --python     # seulement ceux en Python / notebooks
  .venv/bin/python -m rnd.install_reference --max-mb 300 # supprime les copies trop lourdes

Rien n'est exécuté : le code est seulement téléchargé pour être lu par les agents.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEST = REPO / "reference_code"
DB = REPO / "bibliotheque_github" / "bibliotheque_github.sqlite"
PERMISSIVES = ("MIT", "Apache", "BSD", "ISC", "CC0", "Unlicense", "MPL", "0BSD", "Zlib")


def regle(lic):
    if lic and lic.startswith(PERMISSIVES):
        return "permissive", "Inspiration de près autorisée, en citant la source."
    if lic and "GPL" in lic:
        return "copyleft", "Idées seulement : ne rien recopier (sinon Jarvis devient GPL)."
    return "inconnue", "Lecture seulement : pas de licence claire."


def taille_mb(p):
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink()) / 1e6


def neutraliser(d: Path) -> None:
    """Code non fiable : pas de liens symboliques (pourraient pointer hors du dossier) et
    pas de consignes pour agents IA (CLAUDE.md, .claude/) que Claude Code chargerait seul."""
    for p in sorted(d.rglob("*"), key=lambda x: -len(x.parts)):
        if p.is_symlink():
            p.unlink()
        elif p.is_dir() and p.name == ".claude":
            p.rename(p.with_name("_claude_desactive"))
        elif p.is_file() and p.name in ("CLAUDE.md", "CLAUDE.local.md"):
            p.rename(p.with_name(p.name + ".lu-seulement"))


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--python", action="store_true")
    a.add_argument("--max-mb", type=float, default=0)
    a = a.parse_args()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True); con.row_factory = sqlite3.Row
    sql = "SELECT * FROM repos WHERE tier=0"
    if a.python:
        sql += " AND language IN ('Python','Jupyter Notebook','Cython')"
    repos = [dict(r) for r in con.execute(sql + " ORDER BY category, score DESC")]
    DEST.mkdir(exist_ok=True)
    manifeste, echecs = [], []
    for i, r in enumerate(repos, 1):
        d = DEST / r["name"].replace("/", "__")
        if not d.exists():
            print(f"[{i}/{len(repos)}] {r['name']}", flush=True)
            res = subprocess.run(["git", "clone", "--depth", "1", "--single-branch", "--quiet", r["url"] + ".git", str(d)],
                                 capture_output=True, text=True, timeout=900)
            if res.returncode:
                echecs.append(r["name"]); shutil.rmtree(d, ignore_errors=True); continue
            shutil.rmtree(d / ".git", ignore_errors=True)
            neutraliser(d)
        mb = taille_mb(d)
        if a.max_mb and mb > a.max_mb:
            print(f"  trop lourd ({mb:.0f} Mo), retiré : {r['name']}"); shutil.rmtree(d); continue
        typ, txt = regle(r["license"])
        (d / "_JARVIS_NOTE.txt").write_text(
            f"{r['name']} — {r['url']}\nLicence : {r['license'] or 'aucune'} ({typ})\n{txt}\n"
            "Ne jamais exécuter ce code ni y mettre de clé API. Ne jamais l'importer depuis Jarvis.\n", encoding="utf-8")
        manifeste.append({"name": r["name"], "dossier": d.name, "url": r["url"], "categorie": r["category"],
                          "licence": r["license"], "regle": typ, "langage": r["language"], "etoiles": r["stars"],
                          "description": r["description"], "taille_mb": round(mb, 1)})
    (DEST / "MANIFEST.json").write_text(json.dumps(manifeste, ensure_ascii=False, indent=1), encoding="utf-8")
    lignes = ["# Code de référence (lecture seule)", "",
              "Copies des dépôts ★ de la bibliothèque. Ne jamais importer ni exécuter. Licence : voir la colonne Règle.", ""]
    cat = None
    for m in manifeste:
        if m["categorie"] != cat:
            cat = m["categorie"]; lignes += ["", f"## {cat}", "", "| Dossier | Langage | Règle | Description |", "|---|---|---|---|"]
        lignes.append(f"| `{m['dossier']}` | {m['langage'] or '-'} | {m['regle']} | {(m['description'] or '')[:110].replace('|', '/')} |")
    (DEST / "INDEX.md").write_text("\n".join(lignes), encoding="utf-8")
    total = sum(m["taille_mb"] for m in manifeste)
    print(f"\n{len(manifeste)} dépôts dans reference_code/ ({total/1000:.1f} Go). Échecs : {len(echecs)} {echecs[:10]}")


if __name__ == "__main__":
    main()
