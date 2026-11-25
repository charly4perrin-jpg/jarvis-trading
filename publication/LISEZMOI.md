# Publier Jarvis

## 1. Une fois : créer le dépôt public
1. Sur github.com : **New repository** → nom `jarvis-trading`, **Public**, sans README ni licence (ils sont déjà là).
2. Dans **Settings > Emails**, coche « Keep my email addresses private » et copie ton adresse `…@users.noreply.github.com`.
3. Commite tout dans le dépôt privé, puis :
   ```bash
   zsh publication/prepare_public_repo.sh <ton-adresse-noreply> charly4perrin-jpg/jarvis-trading
   git -C ~/Projects/jarvis-public push -u origin main
   ```
4. Mettre à jour plus tard : relancer la même commande, puis `push` (un commit par mise à jour).

## 2. Une fois : la démo en direct
1. `zsh demo/publish_demo.sh` : publie la démo sur la branche `gh-pages`.
2. Sur GitHub : **Settings > Pages** → Source « Deploy from a branch », branche `gh-pages`, dossier `/ (root)`.
3. La démo est en ligne sur `https://charly4perrin-jpg.github.io/jarvis-trading/ (page rejouée en 35 s ; l'essai en direct est sur `live.html`)`.
4. Mise à jour toutes les heures : remplace `__REPO__` dans `launchd/com.jarvis.demo.plist` par le chemin du projet, copie-le dans `~/Library/LaunchAgents/`, puis `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jarvis.demo.plist`.

La démo ne lit que `data/events.jsonl` et n'en sort qu'une liste blanche de champs nettoyés (voir `demo/export_snapshot.py` et `tests/test_demo_snapshot.py`). Personne ne peut agir sur ton Mac depuis la page.
