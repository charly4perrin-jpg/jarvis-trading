"""Garde-fou déterministe (sans IA) de la boucle R&D.

Il lit le diff d'une branche R&D et refuse au moindre doute : chemin protégé touché,
secret, ordre réel, import de reference_code, tests supprimés, diff trop gros.
Une IA ne peut pas le contourner : il tourne avant et indépendamment des agents.
"""
from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

CONFIG = Path(__file__).with_name("config.json")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _protege(path, motifs):
    p = path.replace("\\", "/")
    return any(fnmatch.fnmatch(p, m) or fnmatch.fnmatch("/" + p, "*/" + m.lstrip("*/")) for m in motifs)


def verifier(repo, base, branche, cfg=None):
    cfg = cfg or json.loads(CONFIG.read_text(encoding="utf-8"))
    violations = []
    stat = _git(repo, "diff", "--numstat", f"{base}...{branche}").strip().splitlines()
    fichiers, lignes = [], 0
    for ligne in stat:
        a, s, f = ligne.split("\t", 2)
        fichiers.append(f)
        lignes += (int(a) if a.isdigit() else 0) + (int(s) if s.isdigit() else 0)
    if not fichiers:
        violations.append("aucun changement")
    for f in fichiers:
        if _protege(f, cfg["chemins_proteges"]):
            violations.append(f"chemin protégé modifié : {f}")
    lim = cfg["limites"]
    if len(fichiers) > lim["fichiers_max"]:
        violations.append(f"trop de fichiers ({len(fichiers)} > {lim['fichiers_max']})")
    if lignes > lim["lignes_max"]:
        violations.append(f"diff trop gros ({lignes} lignes > {lim['lignes_max']})")

    supprimes = _git(repo, "diff", "--name-only", "--diff-filter=DR", f"{base}...{branche}").split()
    for f in supprimes:
        if "test" in f.lower():
            violations.append(f"fichier de test supprimé ou renommé : {f}")

    ajouts_fichiers = _git(repo, "diff", "--name-only", "--diff-filter=AR", f"{base}...{branche}").split()
    for f in ajouts_fichiers:
        for motif, raison in cfg.get("fichiers_interdits", []):
            if fnmatch.fnmatch(f, motif) or fnmatch.fnmatch("/" + f, "*/" + motif.lstrip("*/")):
                violations.append(f"{raison} : {f}")

    diff = _git(repo, "diff", "-U0", f"{base}...{branche}")
    ajoutes, retires, par_fichier, courant = [], [], [], None
    for l in diff.splitlines():
        if l.startswith("+++ "):
            courant = l[6:] if l.startswith("+++ b/") else None
        elif l.startswith("+") and not l.startswith("+++"):
            ajoutes.append(l[1:])
            par_fichier.append((courant or "", l[1:]))
        elif l.startswith("-") and not l.startswith("---"):
            retires.append(l[1:])
    for motif, raison in cfg["motifs_interdits"]:
        rx = re.compile(motif)
        hits = [l.strip()[:120] for l in ajoutes if rx.search(l)]
        if hits:
            violations.append(f"{raison} : « {hits[0]} »")
    # Hors des tests : pas de réseau, de lancement de programme, de lecture de .env, ni de
    # modification dynamique du code qui permettrait de contourner un fichier protégé.
    for motif, raison in cfg.get("motifs_interdits_hors_tests", []):
        rx = re.compile(motif)
        hits = [f"{f} : {l.strip()[:100]}" for f, l in par_fichier
                if not (f.startswith("tests/") or "/tests/" in f) and rx.search(l)]
        if hits:
            violations.append(f"{raison} : « {hits[0]} »")
    def_test = re.compile(r"^\s*(async\s+)?def\s+test_")
    net = sum(bool(def_test.match(l)) for l in ajoutes) - sum(bool(def_test.match(l)) for l in retires)
    if net < 0:
        violations.append(f"{-net} test(s) retiré(s)")
    skip = re.compile(r"pytest\.mark\.skip|pytest\.skip\(|@unittest\.skip|xfail")
    if any(skip.search(l) for l in ajoutes):
        violations.append("test désactivé (skip/xfail) ajouté")
    return {"ok": not violations, "violations": violations, "fichiers": fichiers, "lignes": lignes}


if __name__ == "__main__":
    repo, base, br = sys.argv[1:4]
    r = verifier(repo, base, br)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    sys.exit(0 if r["ok"] else 1)
