"""Bibliothèque GitHub de Jarvis — catalogue local de dépôts utiles, interrogeable hors ligne.

Usage en ligne de commande (depuis ~/Projects/jarvis-trading) :
    python bibliotheque_github/github_library.py stats
    python bibliotheque_github/github_library.py cats
    python bibliotheque_github/github_library.py search "regime detection" --limit 20
    python bibliotheque_github/github_library.py search orderbook --cat Exécution --permissive
    python bibliotheque_github/github_library.py top --cat Risque --limit 15
    python bibliotheque_github/github_library.py show freqtrade/freqtrade
    python bibliotheque_github/github_library.py clone vectorbt/vectorbt     # copie de lecture dans reference_code/
    python bibliotheque_github/github_library.py export --cat Arbitrage --json > arbitrage.json

Usage depuis le code Python (un agent, un notebook…) :
    from bibliotheque_github.github_library import Library
    lib = Library()
    for repo in lib.search("walk forward", limit=10):
        print(repo["name"], repo["stars"], repo["url"])

Aucune dépendance externe : uniquement la bibliothèque standard (sqlite3, FTS5).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "bibliotheque_github.sqlite"
PERMISSIVE = ("MIT", "Apache", "BSD", "ISC", "CC0", "Unlicense", "MPL", "0BSD", "Zlib", "PSF")
COLS = ("name", "url", "stars", "forks", "language", "pushed", "created", "license",
        "category", "tier", "score", "topics", "description", "archived", "branch")


def _fts_query(text: str) -> str:
    words = re.findall(r"[\w\-]+", text.lower())
    return " ".join(f'"{w}"*' for w in words) if words else '""'


class Library:
    def __init__(self, path: str | os.PathLike = DB_PATH):
        if not Path(path).exists():
            raise FileNotFoundError(f"Base introuvable : {path}")
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row

    def _rows(self, sql, args=()):
        return [dict(r) for r in self.con.execute(sql, args)]

    def _filters(self, cat, lang, min_stars, permissive, tier, active_since):
        w, a = [], []
        if cat:
            w.append("r.category LIKE ?"); a.append(f"%{cat}%")
        if lang:
            w.append("lower(r.language) = lower(?)"); a.append(lang)
        if min_stars:
            w.append("r.stars >= ?"); a.append(min_stars)
        if permissive:
            w.append("(" + " OR ".join("r.license LIKE ?" for _ in PERMISSIVE) + ")")
            a += [p + "%" for p in PERMISSIVE]
        if tier is not None:
            w.append("r.tier <= ?"); a.append(tier)
        if active_since:
            w.append("r.pushed >= ?"); a.append(active_since)
        return w, a

    def search(self, text="", cat=None, lang=None, min_stars=0, permissive=False,
               tier=None, active_since=None, limit=25, order="relevance"):
        w, a = self._filters(cat, lang, min_stars, permissive, tier, active_since)
        cols = ", ".join("r." + c for c in COLS)
        if text.strip():
            sql = (f"SELECT {cols}, bm25(repos_fts) AS rel FROM repos_fts "
                   f"JOIN repos r ON r.rowid = repos_fts.rowid WHERE repos_fts MATCH ?")
            a = [_fts_query(text)] + a
            if w:
                sql += " AND " + " AND ".join(w)
            sort = {"stars": "r.stars DESC", "pushed": "r.pushed DESC"}.get(order, "rel + (-r.score*0.8)")
        else:
            sql = f"SELECT {cols} FROM repos r" + (" WHERE " + " AND ".join(w) if w else "")
            sort = {"stars": "r.stars DESC", "pushed": "r.pushed DESC"}.get(order, "r.tier, r.score DESC")
        return self._rows(f"{sql} ORDER BY {sort} LIMIT ?", a + [limit])

    def top(self, cat=None, limit=20, **kw):
        return self.search("", cat=cat, limit=limit, **kw)

    def get(self, name):
        r = self._rows("SELECT * FROM repos WHERE lower(name)=lower(?)", (name,))
        return r[0] if r else None

    def categories(self):
        return self._rows("SELECT category, count(*) AS n, sum(tier=0) AS top FROM repos GROUP BY category ORDER BY n DESC")

    def stats(self):
        return dict(self.con.execute(
            "SELECT count(*) total, sum(tier=0) top, sum(tier=1) interessant, sum(tier=2) a_explorer, "
            "max(pushed) derniere_activite FROM repos").fetchone()) | dict(self.con.execute(
            "SELECT value AS genere_le FROM meta WHERE key='generated'").fetchone() or {})

    def clone(self, name, dest=None):
        """Copie de lecture (git clone --depth 1). N'exécute rien : à lire, pas à lancer."""
        repo = self.get(name)
        if not repo:
            raise KeyError(f"{name} n'est pas dans la bibliothèque")
        dest = Path(dest or Path.cwd() / "reference_code") / name.replace("/", "__")
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", repo["url"] + ".git", str(dest)], check=True)
        lic = repo["license"] or "aucune"
        warn = ""
        if "GPL" in lic:
            warn = "Licence copyleft : inspiration seulement, ne pas copier le code dans Jarvis."
        elif lic in ("", "NOASSERTION", "aucune"):
            warn = "Pas de licence claire : lecture seulement."
        (dest / "_JARVIS_NOTE.txt").write_text(
            f"Copie de référence de {name}\nLicence : {lic}\n{warn}\n"
            "Ne pas exécuter ce code sans l'avoir relu. Ne jamais y mettre de clé API.\n", encoding="utf-8")
        return dest


def _print(rows, verbose=False):
    if not rows:
        print("Aucun résultat."); return
    for r in rows:
        star = "★" if r.get("tier") == 0 else " "
        lic = r.get("license") or "?"
        print(f"{star} {r['name']:<45} {r['stars']:>7}★  {r.get('language') or '-':<11} {r['pushed'][:7]}  {lic:<12} {r['category'].split(' (')[0]}")
        if verbose and r.get("description"):
            print(f"    {r['description'][:150]}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Bibliothèque GitHub de Jarvis")
    p.add_argument("--db", default=str(DB_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--cat"); sp.add_argument("--lang"); sp.add_argument("--min-stars", type=int, default=0)
        sp.add_argument("--permissive", action="store_true", help="MIT/Apache/BSD… seulement")
        sp.add_argument("--top", action="store_true", help="seulement les ★ à regarder en premier")
        sp.add_argument("--since", help="actif depuis AAAA-MM-JJ")
        sp.add_argument("--sort", choices=["relevance", "stars", "pushed"], default="relevance")
        sp.add_argument("--limit", type=int, default=25); sp.add_argument("--json", action="store_true")
        sp.add_argument("-v", "--verbose", action="store_true")

    s = sub.add_parser("search"); s.add_argument("text"); common(s)
    t = sub.add_parser("top"); common(t)
    e = sub.add_parser("export"); common(e)
    sh = sub.add_parser("show"); sh.add_argument("name")
    c = sub.add_parser("clone"); c.add_argument("name"); c.add_argument("--dest")
    sub.add_parser("cats"); sub.add_parser("stats")
    a = p.parse_args(argv)
    lib = Library(a.db)

    if a.cmd in ("search", "top", "export"):
        lim = 100000 if a.cmd == "export" and a.limit == 25 else a.limit
        rows = lib.search(getattr(a, "text", ""), cat=a.cat, lang=a.lang, min_stars=a.min_stars,
                          permissive=a.permissive, tier=0 if a.top else None, active_since=a.since,
                          limit=lim, order=a.sort)
        if a.json or a.cmd == "export":
            json.dump(rows, sys.stdout, ensure_ascii=False, indent=1); print()
        else:
            _print(rows, a.verbose)
    elif a.cmd == "show":
        r = lib.get(a.name)
        print(json.dumps(r, ensure_ascii=False, indent=2) if r else "Introuvable.")
    elif a.cmd == "clone":
        print(f"Copié dans {lib.clone(a.name, a.dest)}")
    elif a.cmd == "cats":
        for r in lib.categories():
            print(f"{r['n']:>6}  ({r['top']:>3} ★)  {r['category']}")
    elif a.cmd == "stats":
        print(json.dumps(lib.stats(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
