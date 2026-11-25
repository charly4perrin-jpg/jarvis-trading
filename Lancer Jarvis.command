#!/bin/bash
# Jarvis — installation et lancement en un double-clic (Mac).
# Mode papier uniquement : argent fictif, aucun compte, aucune clé, aucun ordre réel.
# Tout reste dans ce dossier : .venv (programme) et data/ (vos données d'essai).

cd "$(dirname "$0")" || exit 1

pause_et_quitter() {
  echo
  read -r -p "Appuyez sur Entrée pour fermer cette fenêtre." _
  exit "${1:-1}"
}

echo "=================================================="
echo "  Jarvis — suivi de portefeuille et laboratoire"
echo "  de trading SIMULÉ (argent fictif, aucun ordre réel)"
echo "=================================================="
echo

# 1. Trouver Python 3.11 ou plus récent
PY=""
for cand in python3.13 python3.12 python3.11 python3 \
            /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
  if command -v "$cand" >/dev/null 2>&1 &&
     "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Il manque Python (version 3.11 ou plus récente)."
  echo "La page de téléchargement va s'ouvrir : prenez « macOS 64-bit universal2 installer »,"
  echo "installez-le, puis double-cliquez à nouveau sur « Lancer Jarvis »."
  open "https://www.python.org/downloads/macos/"
  pause_et_quitter 1
fi

# 2. Installation (une seule fois, environ 1 à 3 minutes, connexion internet nécessaire)
if [ ! -f .venv/.jarvis-installe ]; then
  echo "Première ouverture : installation de Jarvis dans ce dossier…"
  rm -rf .venv
  "$PY" -m venv .venv || { echo "Échec de la création de l'environnement Python."; pause_et_quitter 1; }
  .venv/bin/python -m pip install --quiet --upgrade pip >/dev/null 2>&1
  if ! .venv/bin/python -m pip install --quiet -e .; then
    echo "L'installation a échoué. Vérifiez votre connexion internet puis relancez."
    pause_et_quitter 1
  fi
  touch .venv/.jarvis-installe
  echo "Installation terminée."
  echo
fi

mkdir -p data
unset OPENAI_API_KEY XAI_API_KEY   # démonstration : aucune clé utilisée

# 3. Démo hors ligne (la première fois seulement) : un refus, un achat papier, un stop
if [ ! -d data/demo_run ]; then
  echo "Démonstration rejouable (hors ligne, argent fictif) :"
  echo "--------------------------------------------------"
  .venv/bin/python -m trading_agent demo --dir data/demo_run
  echo "--------------------------------------------------"
  echo
fi

# 4. Choisir un port libre : 8765, sinon le suivant (si un autre Jarvis tourne déjà)
PORT=""
for p in 8765 8766 8767 8768 8769 8770 8771 8772 8773 8774 8775; do
  if ! .venv/bin/python -c "import socket,sys; s=socket.socket(); sys.exit(s.connect_ex(('127.0.0.1',$p))==0)"; then
    continue
  fi
  PORT=$p; break
done
if [ -z "$PORT" ]; then
  echo "Aucun port libre entre 8765 et 8775. Fermez une autre fenêtre Jarvis puis relancez."
  pause_et_quitter 1
fi

URL="http://127.0.0.1:$PORT"
echo "Ouverture de l'interface : $URL"
echo "Pour arrêter Jarvis : fermez cette fenêtre (ou Ctrl+C)."
echo

# Ouvre le navigateur dès que l'interface répond
( for _ in $(seq 1 60); do
    if curl -s -o /dev/null "$URL"; then open "$URL"; exit 0; fi
    sleep 1
  done ) &

.venv/bin/python -m trading_agent ui --port "$PORT"
pause_et_quitter 0
