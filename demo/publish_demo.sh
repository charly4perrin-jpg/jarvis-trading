#!/bin/zsh
# Publie la démo publique sur GitHub Pages (branche gh-pages du dépôt public).
# Lancé toutes les heures par launchd/com.jarvis.demo.plist, ou à la main :  zsh demo/publish_demo.sh
# Chaque publication remplace la précédente (un seul commit, aucun historique qui gonfle).
set -euo pipefail
cd "$(dirname "$0")/.."
PUBLIC="${JARVIS_PUBLIC_REPO:-$HOME/Projects/jarvis-public}"   # clone du dépôt public (voir publication/LISEZMOI.md)
[ -d "$PUBLIC/.git" ] || { echo "Dépôt public introuvable : $PUBLIC"; exit 1; }
.venv/bin/python -m demo.export_snapshot --out demo/site/snapshot.json
TMP=$(mktemp -d)
cp demo/site/index.html demo/site/live.html demo/site/snapshot.json "$TMP/"
touch "$TMP/.nojekyll"
REPO_URL=$(git -C "$PUBLIC" remote get-url origin)
SLUG=$(echo "$REPO_URL" | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')
sed -i '' "s#__GITHUB_REPO__#$SLUG#" "$TMP/index.html" "$TMP/live.html"
cd "$TMP"
git init -q -b gh-pages
git -c user.name="$(git -C "$PUBLIC" config user.name)" -c user.email="$(git -C "$PUBLIC" config user.email)" add -A
git -c user.name="$(git -C "$PUBLIC" config user.name)" -c user.email="$(git -C "$PUBLIC" config user.email)" commit -qm "Démo : instantané $(date -u +%Y-%m-%dT%H:%MZ)"
git push -q --force "$REPO_URL" gh-pages
cd / && rm -rf "$TMP"
echo "Démo publiée : https://$(echo "$SLUG" | cut -d/ -f1).github.io/$(echo "$SLUG" | cut -d/ -f2)/"
