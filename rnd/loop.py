"""Boucle R&D autonome de Jarvis : des agents Claude Code proposent, codent, testent,
critiquent et valident des améliorations, sans intervention humaine.

Un cycle :
  1. Mission  : la prochaine de rnd/state/missions.json, sinon l'Éclaireur en invente.
  2. Branche  : worktree git isolé `rnd/<id>` à partir de main. Le dépôt principal n'est pas touché.
  3. Chercheur + Développeur : étudient reference_code/ et écrivent le code + les tests.
  4. Garde-fou déterministe (rnd/guard.py) : refuse chemins protégés, secrets, ordres réels…
  5. Tests : pytest + scénarios extrêmes dans le worktree. Tout doit passer.
  6. Critique (adversarial) puis Validateur (décision) : deux agents en lecture seule.
  7. Fusion dans main avec un tag de restauration, retests sur main, retour arrière automatique si échec.
  8. Rapport dans rnd/state/rapports/ + notification macOS.

Commandes :
  python -m rnd.loop                 # lance jusqu'à max_cycles_par_nuit cycles
  python -m rnd.loop --une-fois      # un seul cycle
  python -m rnd.loop --a-blanc       # tout sauf la fusion
  python -m rnd.loop --mission "..." # impose une mission
  touch rnd/state/STOP               # arrêt d'urgence de la R&D (rm pour reprendre)
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

ICI = Path(__file__).resolve().parent
CFG = json.loads((ICI / "config.json").read_text(encoding="utf-8"))
REPO = Path(os.path.expanduser(CFG["repo"])).resolve()
ETAT = REPO / "rnd" / "state"
WT = Path(os.path.expanduser(CFG["worktrees_dir"]))
AGENTS = REPO / ".claude" / "agents"
sys.path.insert(0, str(ICI.parent))
from rnd.guard import verifier  # noqa: E402

# Chemin ABSOLU du Python du projet : le worktree ne contient pas de lien vers .venv, pour
# qu'aucun agent ne puisse modifier l'environnement (bibliothèques) utilisé par l'essai.
PY = str(REPO / CFG["python"])
OUTILS = {
    "eclaireur":   f"Read,Grep,Glob,Bash({PY} bibliotheque_github/github_library.py:*)",
    "developpeur": f"Read,Grep,Glob,Edit,Write,Bash({PY} -m pytest:*),Bash({PY} bibliotheque_github/github_library.py:*),Bash(git diff:*),Bash(git status:*)",
    "critique":    "Read,Grep,Glob",
    "validateur":  "Read,Grep,Glob",
}


def maintenant():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    ETAT.mkdir(parents=True, exist_ok=True)
    ligne = f"[{maintenant()}] {msg}"
    print(ligne, flush=True)
    with open(ETAT / "rnd.log", "a", encoding="utf-8") as f:
        f.write(ligne + "\n")


def sh(cmd, cwd=REPO, timeout=1800, check=False, env=None):
    r = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, **(env or {})})
    if check and r.returncode:
        raise RuntimeError(f"{cmd} → {r.returncode}\n{r.stderr[-2000:]}")
    return r


def git(*a, cwd=REPO, check=True):
    return sh(["git", *a], cwd=cwd, check=check).stdout.strip()


def notifier(titre, texte):
    if sys.platform == "darwin":
        t = texte.replace('"', "'")[:200]
        sh(["osascript", "-e", f'display notification "{t}" with title "{titre}"'])


def agent(nom, consigne, cwd, timeout=None):
    """Lance un agent Claude Code en mode headless, avec son rôle (.claude/agents/<nom>.md)."""
    role = (AGENTS / f"{nom}.md").read_text(encoding="utf-8")
    role = re.sub(r"^---.*?---\s*", "", role, flags=re.S)
    consigne = (f"Le Python du projet est {PY} (utilise ce chemin exact pour lancer les tests ou la "
                f"bibliothèque ; il n'y a pas de dossier .venv dans ce répertoire).\n\n" + consigne)
    cmd = ["claude", "-p", consigne, "--output-format", "json", "--model", CFG["modele"],
           "--append-system-prompt", role, "--allowedTools", OUTILS[nom],
           "--disallowedTools", ",".join(CFG.get("outils_interdits", [])),
           "--permission-mode", "acceptEdits" if nom == "developpeur" else "default"]
    for d in LIENS:   # lecture du code de référence lié au worktree (le lien pointe hors du worktree)
        if (REPO / d).exists():
            cmd += ["--add-dir", str(REPO / d)]
    r = sh(cmd, cwd=cwd, timeout=timeout or CFG["timeout_agent_s"])
    try:
        out = json.loads(r.stdout)
        texte = out.get("result", "")
        if out.get("is_error"):
            raise RuntimeError(texte)
    except json.JSONDecodeError:
        raise RuntimeError(f"agent {nom} : sortie illisible\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
    return texte


def json_final(texte):
    """Récupère le dernier objet JSON d'une réponse d'agent."""
    blocs = re.findall(r"\{.*\}", texte, flags=re.S)
    for b in reversed(blocs):
        for debut in [m.start() for m in re.finditer(r"\{", b)]:
            try:
                return json.loads(b[debut:])
            except json.JSONDecodeError:
                continue
    raise ValueError("pas de JSON final")


def missions():
    ETAT.mkdir(parents=True, exist_ok=True)
    f = ETAT / "missions.json"
    if not f.exists():
        shutil.copy(ICI / "missions_initiales.json", f)
    return json.loads(f.read_text(encoding="utf-8"))


def sauver_missions(m):
    (ETAT / "missions.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def prochaine_mission(imposee=None):
    m = missions()
    if imposee:
        existante = next((x for x in m if x["titre"] == imposee), None)
        if existante is None:   # mission nouvelle ; sinon on garde son détail et ses exigences
            existante = {"titre": imposee, "detail": imposee, "source": "humain"}
            m.append(existante)
        existante["statut"] = "en_cours"
        sauver_missions(m); return existante
    for x in m:
        if x["statut"] == "a_faire":
            x["statut"] = "en_cours"; sauver_missions(m); return x
    log("Plus de mission : l'Éclaireur en cherche de nouvelles.")
    idees = agent("eclaireur", textwrap.dedent(f"""
        Missions déjà traitées : {json.dumps([x['titre'] for x in m], ensure_ascii=False)}
        Propose 3 nouvelles missions. Réponds en terminant par un JSON :
        {{"missions": [{{"titre": "...", "detail": "...", "inspiration": ["owner/repo", ...]}}]}}"""), REPO)
    for x in json_final(idees)["missions"][:3]:
        m.append({**x, "statut": "a_faire", "source": "eclaireur"})
    sauver_missions(m)
    return prochaine_mission()


def finir_mission(mission, statut, rapport):
    m = missions()
    for x in m:
        if x["titre"] == mission["titre"] and x["statut"] == "en_cours":
            x["statut"], x["rapport"] = statut, rapport
    sauver_missions(m)


def tests(cwd):
    sorties = []
    venv = REPO / CFG["python"]
    for d in Path(cwd).rglob("__pycache__"):  # jamais de bytecode périmé
        if ".venv" not in d.parts and "reference_code" not in d.parts:
            shutil.rmtree(d, ignore_errors=True)
    for c in CFG["commandes_tests"]:
        c = c.replace(CFG["python"], str(venv))
        r = sh(c, cwd=cwd, timeout=3600, env={"PYTHONDONTWRITEBYTECODE": "1"})
        sorties.append(f"$ {c}\n{(r.stdout + r.stderr)[-3000:]}")
        if r.returncode:
            return False, "\n".join(sorties)
    return True, "\n".join(sorties)


def nb_tests(sortie):
    m = re.search(r"(\d+) passed", sortie)
    return int(m.group(1)) if m else 0


LIENS = ("reference_code", "bibliotheque_github")   # jamais .venv : voir PY ci-dessus


def lier(chemin):
    """Rend reference_code, la bibliothèque et le venv (non versionnés) visibles dans le worktree."""
    for d in LIENS:
        if (REPO / d).exists() and not (chemin / d).exists():
            (chemin / d).symlink_to(REPO / d)


def delier(chemin):
    """Retire ces liens avant tout commit : ils ne doivent jamais entrer dans git."""
    for d in LIENS:
        if (chemin / d).is_symlink():
            (chemin / d).unlink()


ESSAIS = ETAT / "essais"   # dernier essai rejeté par mission (code + raisons), pour le corriger au lieu de tout réécrire


def _cle(mission):
    import hashlib
    return hashlib.sha1(mission["titre"].encode("utf-8")).hexdigest()[:12]


def essai_precedent(mission, chemin):
    """Réapplique le dernier essai rejeté (déjà passé par le garde-fou) et rend la consigne
    qui l'accompagne. Le garde-fou repassera de toute façon sur le résultat final."""
    patch, info = ESSAIS / f"{_cle(mission)}.patch", ESSAIS / f"{_cle(mission)}.json"
    if not info.exists():
        return ""
    raisons = json.loads(info.read_text(encoding="utf-8")).get("raisons", "")
    applique = patch.exists() and sh(["git", "apply", str(patch)], cwd=chemin).returncode == 0
    if applique:
        return (f"\nUN ESSAI PRÉCÉDENT A ÉTÉ REJETÉ pour : {raisons}\n"
                "Son code est déjà présent dans ce répertoire. Pars de ce code et corrige précisément ces "
                "défauts, avec des tests qui les couvrent ; ne réécris pas tout, et ne casse pas ce qui marchait.\n")
    return (f"\nUN ESSAI PRÉCÉDENT A ÉTÉ REJETÉ pour : {raisons}\n"
            "Son code n'a pas pu être réappliqué : évite ces défauts dans ta version.\n")


def garder_essai(mission, base, branche, raisons):
    ESSAIS.mkdir(parents=True, exist_ok=True)
    (ESSAIS / f"{_cle(mission)}.patch").write_text(git("diff", base, branche) + "\n", encoding="utf-8")
    (ESSAIS / f"{_cle(mission)}.json").write_text(json.dumps(
        {"titre": mission["titre"], "raisons": raisons[:4000], "le": maintenant()}, ensure_ascii=False), encoding="utf-8")


def oublier_essai(mission):
    for ext in ("patch", "json"):
        (ESSAIS / f"{_cle(mission)}.{ext}").unlink(missing_ok=True)


def cycle(mission_imposee=None, a_blanc=False):
    ident = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    mission = prochaine_mission(mission_imposee)
    log(f"Cycle {ident} — mission : {mission['titre']}")
    branche, chemin = f"rnd/{ident}", WT / ident
    base = git("rev-parse", "main")
    WT.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-q", "-b", branche, str(chemin), "main")
    lier(chemin)
    rapport = {"id": ident, "mission": mission, "debut": maintenant(), "etapes": []}
    verdict = "rejetee"
    garde_ok = False
    try:
        ok_base, sortie_base = tests(chemin)
        if not ok_base:
            raise RuntimeError("les tests échouent déjà sur main : R&D suspendue")
        tests_avant = nb_tests(sortie_base)
        precedent = essai_precedent(mission, chemin)
        if precedent:
            rapport["reprise"] = precedent.strip()[:1500]

        resume = agent("developpeur", textwrap.dedent(f"""
            MISSION : {mission['titre']}
            DÉTAIL : {mission.get('detail', '')}
            INSPIRATION POSSIBLE : {', '.join(mission.get('inspiration', [])) or 'à chercher dans la bibliothèque'}
            Le projet compte actuellement {tests_avant} tests qui passent.
            FICHIERS INTERDITS (tout changement sera rejeté automatiquement) : {', '.join(CFG['chemins_proteges'])}.
            Si la mission demande un branchement dans l'un d'eux (endpoint du serveur, agents, configuration),
            ne le fais pas : écris le module et ses tests dans des fichiers autorisés, et décris le branchement
            à faire par une personne dans "a_brancher".
            Termine par un JSON : {{"resume": "...", "fichiers": [...], "sources": ["owner/repo"], "risques": "...", "a_brancher": "..."}}""") + precedent, chemin)
        rapport["etapes"].append({"developpeur": resume[-3000:]})
        delier(chemin)
        git("add", "-A", cwd=chemin)
        lier(chemin)
        if not git("status", "--porcelain", cwd=chemin):
            raise RuntimeError("le développeur n'a rien produit")
        git("-c", "user.name=Jarvis R&D", "-c", "user.email=rnd@jarvis.local", "commit", "-q", "-m",
            f"R&D {ident} : {mission['titre']}", cwd=chemin)

        garde = verifier(REPO, base, branche, CFG)
        rapport["etapes"].append({"garde_fou": garde})
        if not garde["ok"]:
            raise RuntimeError("garde-fou : " + " ; ".join(garde["violations"]))
        garde_ok = True

        ok, sortie = tests(chemin)
        rapport["etapes"].append({"tests": sortie[-4000:]})
        if not ok:
            raise RuntimeError("tests en échec sur la branche")
        if nb_tests(sortie) < tests_avant:
            raise RuntimeError(f"moins de tests qu'avant ({nb_tests(sortie)} < {tests_avant})")

        diff = git("diff", f"{base}...{branche}")[:60000]
        contexte = f"MISSION : {mission['titre']}\n{mission.get('detail','')}\n\nRÉSUMÉ DU DÉVELOPPEUR :\n{resume[-2500:]}\n\nTESTS :\n{sortie[-2500:]}\n\nDIFF :\n{diff}"
        critique = agent("critique", contexte + '\n\nTermine par un JSON : {"bloquants": [...], "mineurs": [...]}', chemin, 1200)
        rapport["etapes"].append({"critique": critique[-3000:]})
        crit = json_final(critique)
        valid = agent("validateur", contexte + f"\n\nCRITIQUE :\n{critique[-3000:]}\n\n"
                      'Termine par un JSON : {"verdict": "accepter" | "refuser", "raisons": [...]}', chemin, 1200)
        v = json_final(valid)
        rapport["etapes"].append({"validateur": v})
        if crit.get("bloquants") or v.get("verdict") != "accepter":
            raise RuntimeError("refusée par le validateur : " + " ; ".join(v.get("raisons", []) + crit.get("bloquants", [])))

        if a_blanc:
            verdict = "acceptee_a_blanc"
        elif not CFG.get("fusion_automatique", False):
            # Une personne relit et fusionne : python -m rnd.loop --fusionner <id>
            verdict = "acceptee_en_attente"
        else:
            fusionner_branche(ident, branche, mission["titre"])
            verdict = "fusionnee"
    except Exception as e:  # tout échec = rejet, rien ne part dans main
        rapport["erreur"] = str(e)[:3000]
        log(f"Rejet : {str(e)[:300]}")
        if garde_ok:   # seul un essai déjà accepté par le garde-fou est gardé pour être corrigé
            try:
                garder_essai(mission, base, branche, str(e))
            except Exception as exc:
                log(f"Essai non gardé : {exc}")
    finally:
        rapport["verdict"], rapport["fin"] = verdict, maintenant()
        delier(chemin)
        sh(["git", "worktree", "remove", "--force", str(chemin)])
        if verdict not in ("fusionnee", "acceptee_en_attente"):
            sh(["git", "branch", "-D", branche])
        elif verdict == "fusionnee":
            sh(["git", "branch", "-d", branche])
        if verdict in ("fusionnee", "acceptee_en_attente"):
            oublier_essai(mission)
        finir_mission(mission, verdict, rapport["id"])
        ecrire_rapport(rapport)
    log(f"Cycle {ident} terminé : {verdict}")
    return verdict


def fusionner_branche(ident, branche, titre):
    """Fusion dans main avec tag de restauration, retests, retour arrière automatique."""
    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("main a des modifications en cours : fusion reportée")
    base = git("rev-parse", "main")
    garde = verifier(REPO, base, branche, CFG)        # le garde-fou repasse juste avant la fusion
    if not garde["ok"]:
        raise RuntimeError("garde-fou : " + " ; ".join(garde["violations"]))
    tag = f"avant-rnd-{ident}"
    git("tag", "-f", tag, "main")
    git("checkout", "-q", "main")
    git("-c", "user.name=Jarvis R&D", "-c", "user.email=rnd@jarvis.local",
        "merge", "--no-ff", "-q", branche, "-m", f"Fusion R&D {ident} : {titre}")
    ok_main, _ = tests(REPO)
    if not ok_main:
        git("reset", "--hard", "-q", tag)
        raise RuntimeError(f"tests en échec après fusion : retour automatique à {tag}")
    if CFG.get("redemarrer_apres_fusion"):
        sh(CFG["commande_redemarrage"])


def ecrire_rapport(r):
    d = ETAT / "rapports"; d.mkdir(parents=True, exist_ok=True)
    (d / f"{r['id']}.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    titre = {"fusionnee": "✅ Amélioration fusionnée", "acceptee_a_blanc": "🧪 Acceptée (à blanc)", "acceptee_en_attente": "✅ Acceptée, en attente de relecture humaine"}.get(r["verdict"], "❌ Proposition rejetée")
    md = [f"# {titre}", f"**Mission :** {r['mission']['titre']}", f"**Début :** {r['debut']} — **fin :** {r['fin']}"]
    if r.get("erreur"):
        md.append(f"**Raison :** {r['erreur'][:800]}")
    for e in r["etapes"]:
        for k, v in e.items():
            md.append(f"\n## {k}\n```\n{(json.dumps(v, ensure_ascii=False, indent=1) if not isinstance(v, str) else v)[:3000]}\n```")
    (d / f"{r['id']}.md").write_text("\n".join(md), encoding="utf-8")
    notifier("Jarvis R&D", f"{titre} — {r['mission']['titre']}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--une-fois", action="store_true")
    p.add_argument("--a-blanc", action="store_true")
    p.add_argument("--mission")
    p.add_argument("--fusionner", metavar="ID", help="fusionne une amélioration acceptée et relue (rnd/<ID>)")
    a = p.parse_args()
    ETAT.mkdir(parents=True, exist_ok=True)
    verrou = open(ETAT / "boucle.lock", "w")   # gardé ouvert jusqu'à la fin : une seule boucle à la fois
    try:
        fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Une autre boucle R&D tourne déjà : rien lancé."); return
    if a.fusionner:
        branche = f"rnd/{a.fusionner}"
        if git("rev-parse", "--verify", "--quiet", branche, check=False) == "":
            log(f"Branche {branche} introuvable."); return
        try:
            fusionner_branche(a.fusionner, branche, f"amélioration {a.fusionner} relue par une personne")
            sh(["git", "branch", "-d", branche])
            log(f"Fusion de {branche} : faite, tests verts.")
        except Exception as e:
            log(f"Fusion de {branche} refusée : {e}")
        return
    if (ETAT / "STOP").exists():
        log("STOP présent : R&D arrêtée."); return
    if not shutil.which("claude"):
        log("Claude Code introuvable dans le PATH."); return
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        log("Le dépôt n'est pas sur main : R&D reportée."); return
    n = 1 if (a.une_fois or a.mission) else CFG["max_cycles_par_nuit"]
    for i in range(n):
        if (ETAT / "STOP").exists():
            break
        try:
            cycle(a.mission if i == 0 else None, a.a_blanc)
        except Exception as e:  # une erreur arrête la nuit, sans rien toucher à main
            log(f"Erreur hors cycle, R&D arrêtée pour cette nuit : {e}")
            notifier("Jarvis R&D", f"Erreur : {str(e)[:120]}")
            break
    try:
        exporter_obsidian()
    except Exception as e:   # la note est un confort : son échec n'arrête rien
        log(f"Note Obsidian non mise à jour : {e}")


VERDICTS = {"fusionnee": "fusionnée", "acceptee_en_attente": "acceptée, à relire",
            "acceptee_a_blanc": "acceptée (à blanc)", "rejetee": "rejetée"}


def exporter_obsidian(dossier=None):
    """Réécrit UNE note générée (« R&D — suivi automatique ») dans le coffre Obsidian :
    missions et derniers rapports. Rien d'autre n'est touché ; aucun secret n'y est écrit
    (titres, statuts et raisons de rejet tronquées seulement)."""
    d = Path(os.path.expanduser(dossier or CFG.get("obsidian_dir", "~/Documents/Obsidian Vault/Jarvis")))
    if not d.is_dir():
        return None
    lignes = ["# R&D — suivi automatique", "",
              f"> Note régénérée par la boucle R&D le {maintenant()}. Ne pas l'éditer : elle sera écrasée. "
              "Retour : [[Accueil]] · [[R&D]]", "", "## Missions", "", "| Mission | Statut |", "|---|---|"]
    for x in missions():
        lignes.append(f"| {x['titre']} | {x.get('statut', '?')} |")
    lignes += ["", "## Derniers cycles", "", "| Date | Mission | Verdict | Raison |", "|---|---|---|---|"]
    rapports = sorted((ETAT / "rapports").glob("*.json"), reverse=True)[:15]
    for f in rapports:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        raison = re.sub(r"[|\n`]", " ", str(r.get("erreur") or ""))[:160]
        lignes.append(f"| {r.get('debut', '')[:16]} | {r.get('mission', {}).get('titre', '?')} | "
                      f"{VERDICTS.get(r.get('verdict'), r.get('verdict'))} | {raison} |")
    cible = d / "R&D — suivi automatique.md"
    cible.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    return cible


if __name__ == "__main__":
    main()
