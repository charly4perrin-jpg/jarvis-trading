# Démo de Jarvis (hors ligne, argent fictif)

**Démo : données préparées, argent fictif.** Les données (horloge, bougies, carnet
d'ordres) sont fixées à l'avance, reprises des scénarios extrêmes. Les décisions, elles,
sont prises en direct par le vrai Coordinator, le vrai moteur de risque (`risk.py`) et
le vrai courtier papier (`paper.py`). Pas de réseau, pas de `.env`, pas d'IA, pas de
notification. Aucun ordre réel.

## Lancer

```bash
.venv/bin/python -m trading_agent demo
```

Autre capital fictif de départ : `--cash 9000` (montants ci-dessous multipliés d'autant ;
le risque reste 0,5 % du capital par trade).

Durée : une seconde. Les fichiers vont dans `data/demo_run/` (base, journal, état
dédiés). La démo refuse de démarrer si son dossier touche un fichier de l'essai
principal (`data/jarvis.db`, `data/events.jsonl`, `data/paper_state.json`…). Chaque
lancement remet `data/demo_run/` à zéro : on repart toujours de 3 000 € fictifs (ou du
montant donné par `--cash`).

## Résultats attendus

1. **Refus.** Un signal d'achat est présent et les bougies sont fraîches, mais le carnet
   d'ordres fourni date d'une heure. Le moteur de risque refuse : « carnet d'ordres
   périmé ou manquant ». Cash : 3 000,00 € avant et après.
2. **Achat papier.** Données fraîches : le signal passe le Contradicteur, la
   proposition, le risque. Carnet utilisé : vente à 130,05 € (100 BTC disponibles).
   Achat de 0,47167471 BTC à 130,05 €, frais 0,15 €, stop 99,00 €. Position ouverte,
   cash 2 938,51 €.
3. **Stop.** La bougie suivante ouvre à 92 €, sous le stop (scénario « Krach éclair ») :
   vente au prix réellement disponible, 91,91 € (92 € moins le glissement), pas à 99 €.
   Frais 0,11 €, résultat −18,25 €. Cash final **2 981,75 €**.

La perte (18,25 €) dépasse le budget de risque prévu (15 €, soit 0,5 %) : c'est voulu,
un trou de prix sous le stop coûte plus que prévu, et la démo le montre honnêtement.

4. **Trade gagnant.** Le marché se stabilise à 90 €, puis un nouveau signal d'achat
   apparaît à 100 €. Achat de 1,27845501 BTC à 100,05 €, frais 0,32 €, stop 89 €. Le
   prix monte jusqu'à 121 € puis redescend : Jarvis vérifie chaque bougie (20 au total)
   et vend quand sa règle de sortie le décide, à 104,95 €. Frais 0,34 €, résultat
   **+5,61 €**. Cash final **2 987,36 €**.

Bilan : un refus, un trade perdant, un trade gagnant, soit −12,64 € au total. La démo
ne cherche pas à finir en gain : elle montre comment Jarvis décide. Aucune stratégie ne
gagne à tous les coups. Ici, la règle de sortie (stratégie de démonstration) rend une
partie du gain : vendue à 105 € après un sommet à 121 €.

## Retrouver le motif du refus

La démo affiche, pour chaque étape, la commande de trace à copier, par exemple :

```bash
.venv/bin/python -m trading_agent trace --log data/demo_run/events.jsonl --thread <identifiant>
```

Dans le fil de l'étape 1, la ligne `risk_manager → coordinator` donne le motif :
« Refusé : carnet d'ordres périmé ou manquant. » La ligne `market_intelligence`
juste avant montre le carnet lu. Aucune ligne `execution` : rien n'a été acheté.

## Vérifier le cash final

Dans les fils des étapes 2 et 3, les lignes `execution → portfolio_manager` donnent
prix, quantité et frais :

- achat : 130,05 × 0,47167471 + 0,15 = 61,49 € dépensés → cash 2 938,51 € ;
- vente : 91,91 × 0,47167471 − 0,11 = 43,24 € reçus → cash 2 981,75 €.

Le détail exact (sans arrondi) est dans `data/demo_run/paper_ledger.jsonl` (champ
`cash_after`).

## Limite connue

Depuis le 2026-11-24, le moteur de risque contrôle aussi l'âge du carnet d'ordres avant
tout achat (30 secondes au plus, réglage `MAX_BOOK_AGE_S`) ; les sorties ne sont jamais
bloquées. Bitvavo et Kraken ne datent pas leurs carnets dans les données publiques : l'âge
est alors compté depuis la réception du carnet par Jarvis.
