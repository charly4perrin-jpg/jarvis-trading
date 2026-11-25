#!/bin/zsh
# Crée (ou met à jour) le dépôt PUBLIC de Jarvis à partir du dernier commit du dépôt privé.
#   zsh publication/prepare_public_repo.sh <email-noreply-github> [utilisateur/depot]
# Exemple : zsh publication/prepare_public_repo.sh 123456+charly4perrin-jpg@users.noreply.github.com charly4perrin-jpg/jarvis-trading
#
# - N'exporte que les fichiers COMMITÉS (git archive) : data/, .env, reference_code/, communication/ ne sortent jamais.
# - Historique public neuf, signé uniquement avec l'adresse noreply : ton Gmail reste privé.
# - Bloque la publication si un secret, un e-mail ou un chemin personnel est détecté.
# - Ne pousse RIEN tout seul : il affiche la commande à lancer quand tu as vérifié.
set -euo pipefail
EMAIL="${1:?Donne ton adresse noreply GitHub (Settings > Emails)}"
SLUG="${2:-charly4perrin-jpg/jarvis-trading}"
NOM_DEPOT="${SLUG#*/}"; UTIL="${SLUG%%/*}"
PRIVE="$(cd "$(dirname "$0")/.." && pwd)"
PUBLIC="${JARVIS_PUBLIC_REPO:-$HOME/Projects/jarvis-public}"
[[ "$EMAIL" == *users.noreply.github.com ]] || { echo "L'adresse doit être ta noreply GitHub."; exit 1; }
if [ -n "$(git -C "$PRIVE" status --porcelain --untracked-files=no)" ]; then
  echo "Des modifications ne sont pas commitées dans le dépôt privé : commite-les d'abord."; exit 1
fi

mkdir -p "$PUBLIC"
STAGE=$(mktemp -d)
git -C "$PRIVE" archive HEAD | tar -x -C "$STAGE"
mkdir -p "$STAGE/docs"
mv "$STAGE/README.md" "$STAGE/docs/README_TECHNIQUE.md"
DEMO="https://$UTIL.github.io/$NOM_DEPOT/"
sed -e "s#__DEMO_URL__#$DEMO#g" -e "s#__GITHUB_REPO__#$SLUG#g" -e "s#__REPO_NAME__#$NOM_DEPOT#g" \
    "$STAGE/publication/README_PUBLIC.md" > "$STAGE/README.md"
rm -f "$STAGE/publication/README_PUBLIC.md"

# Contrôles bloquants
PERSO=$(git -C "$PRIVE" log -1 --format=%ae)
if grep -rInE "(sk|xai)-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY|(API_KEY|PASSWORD|SECRET)=[A-Za-z0-9_-]{8,}" "$STAGE" \
   | grep -v "publication/prepare_public_repo.sh"; then
  echo "SECRET POSSIBLE détecté ci-dessus : publication annulée."; rm -rf "$STAGE"; exit 1
fi
if grep -rIl -e "$PERSO" -e "/Users/$USER" -e "@gmail\.com" "$STAGE"; then
  echo "Donnée personnelle détectée dans les fichiers ci-dessus : publication annulée."; rm -rf "$STAGE"; exit 1
fi

# Remplace le contenu du dépôt public (sauf .git) par l'export
if [ ! -d "$PUBLIC/.git" ]; then git -C "$PUBLIC" init -q -b main; fi
git -C "$PUBLIC" config user.name "$UTIL"
git -C "$PUBLIC" config user.email "$EMAIL"
find "$PUBLIC" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -R "$STAGE"/. "$PUBLIC"/
rm -rf "$STAGE"
git -C "$PUBLIC" add -A
if git -C "$PUBLIC" diff --cached --quiet; then echo "Rien de nouveau à publier."; exit 0; fi
VERSION=$(grep -m1 '^version' "$PUBLIC/pyproject.toml" | cut -d'"' -f2)
git -C "$PUBLIC" commit -qm "Jarvis $VERSION ($(date +%Y-%m-%d))"
git -C "$PUBLIC" remote get-url origin >/dev/null 2>&1 || git -C "$PUBLIC" remote add origin "https://github.com/$SLUG.git"
echo
echo "Dépôt public prêt dans $PUBLIC (auteur : $UTIL <$EMAIL>)."
echo "Vérifie-le, puis publie avec :   git -C \"$PUBLIC\" push -u origin main"
