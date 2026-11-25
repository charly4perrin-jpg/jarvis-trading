# Bibliothèque GitHub de Jarvis

Catalogue local de dépôts GitHub utiles pour Jarvis (trading, données de marché, backtest, risque, exécution, agents IA, infrastructure…), classés selon l'agent qu'ils peuvent renforcer.

## Installation

Copier ce dossier dans le projet :

```
~/Projects/jarvis-trading/bibliotheque_github/
```

Aucune dépendance : Python standard + SQLite (FTS5, inclus dans Python 3.13 sur macOS).

## Contenu

| Fichier | Rôle |
|---|---|
| `bibliotheque_github.sqlite` | La base : table `repos` + index plein texte `repos_fts` + `meta` |
| `bibliotheque_github.csv` | La même chose en tableur (Excel, Numbers, pandas) |
| `github_library.py` | Outil de recherche (ligne de commande + classe `Library`) |

## Ligne de commande

```bash
cd ~/Projects/jarvis-trading
.venv/bin/python bibliotheque_github/github_library.py stats
.venv/bin/python bibliotheque_github/github_library.py cats
.venv/bin/python bibliotheque_github/github_library.py search "regime detection" -v
.venv/bin/python bibliotheque_github/github_library.py search "order book" --cat Exécution --permissive
.venv/bin/python bibliotheque_github/github_library.py top --cat Risque --limit 15
.venv/bin/python bibliotheque_github/github_library.py search kraken --since 2026-01-01 --sort stars
.venv/bin/python bibliotheque_github/github_library.py show freqtrade/freqtrade
.venv/bin/python bibliotheque_github/github_library.py clone polakowo/vectorbt
.venv/bin/python bibliotheque_github/github_library.py export --cat Arbitrage > arbitrage.json
```

`clone` télécharge une copie de lecture (`git clone --depth 1`) dans `reference_code/`, avec une note sur la licence. Ajouter `reference_code/` au `.gitignore` de Jarvis.

## Depuis le code (un agent, un test, un notebook)

```python
from bibliotheque_github.github_library import Library

lib = Library()
lib.search("walk forward", limit=10)              # recherche plein texte
lib.top(cat="Arbitrage", limit=5)                 # meilleurs d'une catégorie
lib.search("sentiment", permissive=True, tier=0)  # seulement les ★, licences permissives
lib.get("ccxt/ccxt")                              # fiche complète
```

La base est ouverte en lecture seule : un agent peut la consulter, jamais la modifier.

## Colonnes

`name, url, stars, forks, language, pushed (dernière activité), created, license, category, tier (0 = ★ à regarder en premier, 1 = intéressant, 2 = à explorer), score, topics, description, archived, branch, found_by (requête qui l'a trouvé), hits (nombre de requêtes qui l'ont trouvé)`

## Règles d'usage pour rester dans l'esprit de Jarvis

- **Licences.** MIT / Apache / BSD : on peut reprendre du code en citant la source. GPL / AGPL : inspiration seulement, sinon Jarvis devient GPL. Sans licence : lecture seulement.
- **Ne rien exécuter sans relecture.** Beaucoup de « bots crypto » sur GitHub contiennent du code de mauvaise qualité, voire malveillant (vol de clés, faux bots d'arbitrage). Ne jamais lancer un dépôt cloné, et ne jamais y mettre de clé API.
- **Une idée reprise passe par le même circuit que le reste** : tests, backtest walk-forward, mode papier, validation humaine.
- Les étoiles et les dates datent de la génération (voir `meta`). Une étoile n'est pas une preuve de qualité.
