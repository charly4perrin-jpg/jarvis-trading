# R&D autonome de Jarvis

> **Installé le 18/11/2026, avec des renforcements par rapport à la version reçue** :
> - fusion **manuelle** par défaut (`"fusion_automatique": false`) : une amélioration acceptée attend sur sa branche `rnd/<id>` ; après relecture : `.venv/bin/python -m rnd.loop --fusionner <id>` (le garde-fou et tous les tests repassent, retour arrière automatique en cas d'échec) ;
> - chemins protégés adaptés aux **vrais** fichiers de Jarvis (`agents.py`, `events.py`, `risk.py`, `store.py`, `adversarial.py`, `resilience.py`, `server.py`, `config.py`, `ai.py`, `notifications.py`, tests de sécurité…) ;
> - garde-fou renforcé hors des tests : pas d'accès réseau, de lancement de programme, de lecture de `.env`, de modification dynamique du code (`setattr`, `importlib`…), de réaffectation d'un élément de sécurité, d'accès direct aux données de l'essai ; pas de nouveau `conftest.py`, `sitecustomize.py` ou `.pth` ;
> - le worktree n'a plus de lien vers `.venv` (sinon un agent aurait pu modifier les bibliothèques de l'essai sans que le garde-fou le voie) ;
> - lecture de `.env`, `~/.ssh`, `~/Library`, `~/.claude` et accès internet interdits aux agents.
> - Tests : `tests/test_rnd_guard.py` et `tests/test_rnd_hardening.py`.
>
> **Pré-requis manquant** : la commande `claude` (Claude Code en ligne de commande) n'est pas installée sur ce Mac ; la boucle ne peut pas encore tourner.

Chaque nuit à 03:15, une équipe d'agents Claude Code améliore Jarvis toute seule :

| Agent | Rôle | Droits |
|---|---|---|
| Éclaireur | Choisit les prochaines missions à partir du code de Jarvis, de `reference_code/` (160 dépôts ★) et de la bibliothèque (11 046 dépôts) | lecture |
| Chercheur-Développeur | Étudie le code de référence, implémente la mission et ses tests, dans un worktree git isolé | écriture dans le worktree seulement |
| Garde-fou (`guard.py`, sans IA) | Refuse : zones protégées, secrets, ordres réels, import de reference_code, tests retirés ou désactivés, diff trop gros | — |
| Tests | 137+ tests et 10 scénarios extrêmes. Tout doit passer, et le nombre de tests ne peut pas baisser | — |
| Critique | Cherche toutes les raisons de refuser (bugs, lookahead, frais ignorés, contournement du risque…) | lecture |
| Validateur | Décide seul. Refuse au moindre doute | lecture |
| Fusion | Tag de restauration `avant-rnd-<id>`, fusion dans main, retests, **retour arrière automatique** si un test casse, redémarrage du serveur | — |

## Ce que les agents ne peuvent jamais toucher

Règles de risque, sécurité et arrêt d'urgence, audit et journal, configuration, dépendances, données de l'essai, plists, et la R&D elle-même (`rnd/`, `.claude/`). Ces chemins sont listés dans `rnd/config.json` (`chemins_proteges`). C'est un programme sans IA qui les vérifie : un agent ne peut pas le convaincre. Pour changer ces zones, c'est toi.

## Commandes

```bash
.venv/bin/python -m rnd.loop --a-blanc --une-fois    # un cycle complet, sans fusion (premier essai)
.venv/bin/python -m rnd.loop --une-fois              # un cycle avec fusion
.venv/bin/python -m rnd.loop --mission "Ajouter ..."  # imposer une mission
touch rnd/state/STOP                                  # arrêter la R&D   (rm rnd/state/STOP pour reprendre)
launchctl bootout gui/$(id -u)/com.jarvis.rnd         # désactiver le lancement nocturne
git log --oneline --grep "Fusion R&D"                 # tout ce que la R&D a fusionné
git reset --hard avant-rnd-<id>                       # revenir avant une fusion précise
```

Rapports : `rnd/state/rapports/<id>.md` (une notification macOS à chaque cycle). Missions : `rnd/state/missions.json` (tu peux en ajouter à la main avec `"statut": "a_faire"`). Journal : `rnd/state/rnd.log`.

## Coût

Chaque cycle consomme ton quota Claude (abonnement) : comptés large, 3 cycles par nuit, c'est l'équivalent de plusieurs longues sessions Claude Code. Ajuste `max_cycles_par_nuit` et `modele` dans `rnd/config.json`.
