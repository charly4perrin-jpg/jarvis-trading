# Suivre son vrai portefeuille (lecture seule)

Jarvis peut valoriser ce que vous détenez vraiment, **sans connecter de compte** : pas de
clé API, pas d'ordre. Vous saisissez vos soldes ; Jarvis lit les cours publics en euros.
Ce fichier (`data/holdings.json`) est séparé de l'essai papier : les 3 000 € fictifs et
leur journal ne sont jamais touchés.

## Saisir un solde

```bash
.venv/bin/python -m trading_agent holdings set --exchange bitvavo --asset BTC --quantity 0.01
```

`--quantity 0` retire la ligne. La même crypto sur deux plateformes donne deux lignes.

## Importer un relevé CSV

```csv
plateforme;actif;quantite;date
bitvavo;BTC;0,01;2026-11-22
kraken;ETH;0,5;2026-11-22
```

```bash
.venv/bin/python -m trading_agent holdings import releve.csv
```

Un relevé est une photo des soldes : l'importer deux fois **remplace** les quantités,
rien n'est doublé.

## Voir la valeur

```bash
.venv/bin/python -m trading_agent holdings show
```

Chaque ligne affiche deux dates : « quantités au … » (votre relevé) et « prix au … »
(le cours). Un cours frais ne veut pas dire que votre solde est à jour : si vous avez
acheté ou vendu depuis, réimportez.

Si un cours manque ou date de plus de 15 minutes, la ligne indique « valorisation
indisponible » et le total devient « Total partiel … ce n'est PAS la valeur totale ».
