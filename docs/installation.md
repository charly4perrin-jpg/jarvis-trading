# Installer Jarvis sur un Mac (testeur, sans ligne de commande)

Jarvis est un **outil de suivi de portefeuille et un laboratoire de trading crypto
simulé**. Ce n'est pas un outil pour gagner de l'argent : tout se passe en argent
fictif, aucun compte n'est connecté, aucune clé n'est demandée, aucun ordre réel
n'est possible.

## Ce qu'il faut

- Un Mac (macOS 12 ou plus récent) et une connexion internet.
- **Python 3.11 ou plus récent.** S'il manque, Jarvis ouvre lui-même la page
  [python.org/downloads/macos](https://www.python.org/downloads/macos/) : prenez
  « macOS 64-bit universal2 installer », double-cliquez sur le fichier téléchargé et
  suivez l'installeur (Continuer, Accepter, Installer).

## Installer et ouvrir

1. Décompressez le dossier `jarvis-testeur.zip` reçu, et rangez le dossier obtenu
   où vous voulez (par exemple dans Documents).
2. Dans ce dossier, double-cliquez sur **« Lancer Jarvis »**.
   - La première fois, macOS peut refuser (« développeur non identifié »). Faites alors
     **clic droit (ou Ctrl+clic) → Ouvrir → Ouvrir**. Si aucun bouton « Ouvrir »
     n'apparaît : Réglages Système → Confidentialité et sécurité → en bas,
     « Ouvrir quand même ».
3. Une fenêtre Terminal s'ouvre et s'occupe de tout. La première fois, l'installation
   prend 1 à 3 minutes, puis une **démonstration** s'affiche : un achat refusé, un
   achat papier, un stop, chacun avec son motif.
4. Le navigateur s'ouvre tout seul sur l'interface (`http://127.0.0.1:8765`).
   Dans le menu du haut : **Portefeuille** (votre portefeuille fictif de 3 000 € et,
   si vous le souhaitez, vos vrais soldes en lecture seule), **Laboratoire**, **Trades**…

Pour **arrêter** Jarvis : fermez la fenêtre Terminal. Pour le **rouvrir** :
double-cliquez à nouveau sur « Lancer Jarvis » (plus d'installation, ça démarre
en quelques secondes).

## Où sont mes données ?

Tout reste dans le dossier de Jarvis : le programme dans `.venv`, vos essais dans
`data/`. Rien n'est envoyé ailleurs ; seuls les cours publics des plateformes sont lus.
Pour **tout recommencer à zéro** : supprimez le dossier `data`. Pour **désinstaller** :
mettez tout le dossier à la corbeille.

## Si ça ne marche pas

| Message | Que faire |
|---|---|
| « Il manque Python » | Installez Python depuis la page qui s'ouvre, puis relancez. |
| « L'installation a échoué » | Vérifiez internet, puis relancez : l'installation reprend. |
| Le navigateur ne s'ouvre pas | Ouvrez à la main l'adresse affichée dans la fenêtre Terminal. |
| L'adresse finit par 8766 (ou plus) | Normal : un autre Jarvis utilise déjà 8765, celui-ci a pris le port suivant. |

## Pour la personne qui envoie Jarvis

Préparez le dossier à envoyer depuis le dépôt (il ne contient ni `data/`, ni `.env`,
ni clé) :

```bash
git archive --format=zip --prefix=jarvis-testeur/ -o ~/Desktop/jarvis-testeur.zip HEAD
```

Si vous l'essayez sur la même machine que l'essai continu (port 8765, dossier `data/`
du dépôt), décompressez-le **ailleurs** : il aura ses propres données et prendra
automatiquement le port 8766.
