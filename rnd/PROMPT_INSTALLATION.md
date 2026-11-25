Copie-colle ce message dans Claude Code, ouvert dans ~/Projects/jarvis-trading, après avoir décompressé le zip à la racine du projet.

---

Installe la R&D autonome et la bibliothèque GitHub que je viens de décompresser à la racine de Jarvis (`rnd/`, `.claude/agents/`, `launchd/`, `tests/test_rnd_guard.py`, `bibliotheque_github/`). Lis d'abord `rnd/LISEZMOI.md` et `bibliotheque_github/LISEZMOI.md`. Fais un point de restauration git avant de commencer.

1. **Chemins protégés.** Lis toute l'arborescence de Jarvis, puis adapte `chemins_proteges` dans `rnd/config.json` aux VRAIS fichiers : moteur de risque, règles de risque et leur config versionnée, agent Sécurité, arrêt d'urgence et « tout vendre », coupe-circuits, Audit et journal chaîné SHA-256, base SQLite et données de l'essai, configuration, LaunchAgent, tout ce qui pourrait un jour passer en réel. Retire les motifs qui bloqueraient à tort du code ordinaire (par exemple si un fichier de stratégie contient « journal » dans son nom). Montre-moi la liste finale.
2. **Commandes de test.** Vérifie que `commandes_tests` correspond aux vraies commandes (pytest et `-m trading_agent scenarios`) et qu'aucune n'écrit dans les données de l'essai.
3. **.gitignore.** Ajoute `reference_code/`, `rnd/state/`, `bibliotheque_github/*.sqlite`, `bibliotheque_github/*.csv`.
4. **Tests.** Lance `tests/test_rnd_guard.py` puis toute la suite : tout doit passer. Ajoute un test qui vérifie que chaque fichier de risque, sécurité et audit réel est bien refusé par le garde-fou.
5. **Code de référence.** Lance `.venv/bin/python -m rnd.install_reference --max-mb 400` (plusieurs Go, ça peut prendre du temps), puis montre-moi le résumé.
6. **Premier essai à blanc.** Lance `.venv/bin/python -m rnd.loop --a-blanc --une-fois`, puis montre-moi le rapport dans `rnd/state/rapports/`.
7. **Nuit.** Si tout est bon : remplace `__REPO__` dans `launchd/com.jarvis.rnd.plist` par le chemin absolu du projet, copie-le dans `~/Library/LaunchAgents/`, charge-le avec `launchctl bootstrap gui/$(id -u) ...`, et vérifie que `claude` est bien trouvé dans un shell `zsh -lc`.
8. Commite le tout avec un message clair.

Ne modifie aucune règle de risque, aucun seuil, ni le mode papier.
