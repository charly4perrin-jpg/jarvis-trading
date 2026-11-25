# Trois pistes (sortie suiveuse, bougies de 4 h, rebond RSI 2), figées AVANT tout code et tout backtest

Rédigé le 2026-11-24 par Claude Code, après validation des trois pistes par l'utilisateur.
Ce fichier est commité avant la première ligne de code et avant toute évaluation : le commit
en fait foi. Aucun seuil officiel n'est changé ; aucun paramètre ne sera retouché après coup ;
un échec est noté tel quel. Les réglages chiffrés viennent de règles publiées, pas d'un
ajustement sur nos données.

## Contexte
Les pistes précédentes (`pistes-volume-expansion.md`) ont échoué. `volume_expansion` reste
à 4/6 (2 fenêtres en perte, Sharpe dégonflé 0,64 à 38 essais). Ici, on change de levier :
la façon de sortir, l'horizon des bougies, et une autre famille de stratégie.

## Piste 1 — Sortie suiveuse « chandelier » (`volume_chandelier`)
- **Hypothèse** : la sortie actuelle (clôture sous l'EMA 20) coupe tôt les grands gagnants.
  Une sortie qui suit le plus haut atteint depuis l'achat laisse courir les tendances et doit
  relever le gain moyen et le Sharpe, au prix de rendre une partie des gains à la sortie.
- **Entrée** : identique à `volume_expansion` (volume ≥ 2 × moyenne 20, bougie haussière
  clôturant dans son tiers haut, clôture > SMA(100)), stop initial = clôture − 2 × ATR(14),
  géré par le courtier papier comme aujourd'hui.
- **Sortie** : signal à la clôture J si clôture J < (plus haut des hauts depuis la bougie du
  premier fill, J comprise) − 3 × ATR(22) ; vente à l'ouverture suivante. Plus de sortie par
  l'EMA 20. Réglage 22 jours / 3 × ATR = chandelier publié (Chuck LeBeau).
- Données insuffisantes (moins de 23 bougies, date d'achat inconnue) : pas de signal de
  sortie (le stop initial reste actif).
- Terrain de régime : identique à `volume_expansion` (haussier et sans tendance, volatilité
  haute permise).

## Piste 2 — `volume_expansion` en bougies de 4 heures
- **Hypothèse** : le même motif (poussée de volume sur une bougie haussière forte) existe à
  4 h ; avec environ six fois plus de bougies, on obtient beaucoup plus de trades et un
  Sharpe mesuré sur plus d'observations, donc un verdict plus fiable.
- **Règle** : stratégie `volume_expansion` inchangée, paramètres identiques en nombre de
  bougies (moyenne de volume 20, SMA 100, EMA 20, ATR 14, stop 2 × ATR).
- **Protocole** : 8 mêmes cryptos, Bitvavo, `--timeframe 4h --days 1825`, 6 fenêtres, mêmes
  6 critères et mêmes seuils, filtre de régime et veto Markov selon les réglages en vigueur.
  Le passage évalue toutes les stratégies en 4 h (nécessaire au Sharpe dégonflé), mais seul
  le verdict de `volume_expansion` compte pour cette piste.
- Limite assumée : l'essai papier tourne en bougies d'un jour. Une validation ici ne
  changerait rien à l'essai sans protocole séparé et décision humaine.

## Piste 3 — Rebond court RSI 2 en tendance (`rsi2_rebound`)
- **Hypothèse** : dans une crypto en tendance de fond haussière, deux jours de forte baisse
  sont souvent suivis d'un rebond rapide (règle publiée par Larry Connors pour les actions).
  Les trades sont courts et nombreux, ce qui doit aider le nombre de trades et le Sharpe.
- **Entrée** : RSI(2) < 10 à la clôture J (RSI de Jarvis : moyenne simple des hausses et
  des baisses sur 2 variations) ET clôture J > SMA(200). Stop = clôture − 2 × ATR(14)
  (convention de Jarvis ; la règle d'origine n'a pas de stop).
- **Sortie** : clôture J > SMA(5) ; vente à l'ouverture suivante.
- Moins de 201 bougies valides : pas d'entrée.
- Terrain de régime : celui par défaut (haussier et sans tendance, volatilité haute exclue).

## Règles d'arrêt (identiques pour les trois pistes)
- Une seule évaluation officielle par piste (pistes 1 et 3 : 8 actifs, journalier, 1825 jours ;
  piste 2 : voir plus haut).
- **Validée** seulement si les 6 critères passent. Elle reste alors « candidate » ; tout
  passage au papier demande un protocole séparé et une décision humaine.
- **Écartée** dès qu'un critère échoue, avec ses chiffres. Pas de variante (autre multiple
  d'ATR, RSI 3, seuil 5 ou 15, bougies de 1 h ou 12 h…), pas de nouvel essai sur ces règles.
- `research_trials` passe de 38 à 41 avant les évaluations.
- Pistes 1 et 3 marquées « backtest seulement » : refusées comme stratégie de l'essai,
  absentes du laboratoire. Résultats dans `data/evaluations-pistes2-20261124/` (journalier)
  et `data/evaluations-pistes2-4h-20261124/` (4 h), jamais dans `data/evaluations/`.

## Résultats (évaluations officielles du 2026-11-24, 8 actifs, 1825 jours, 41 essais)

Aucune des trois pistes ne passe. Aucun paramètre n'a été retouché ; aucun seuil changé.

| Critère | volume_expansion 1 j (référence) | 1. volume_chandelier | 2. volume_expansion 4 h | 3. rsi2_rebound |
|---|---|---|---|---|
| ≥ 100 trades clôturés | ✅ 131 | ✅ 116 | ✅ 891 | ✅ 584 |
| Espérance nette > 0 | ✅ +9,23 €/trade | ✅ +11,20 €/trade | ✅ +1,24 €/trade | ❌ −0,93 €/trade |
| Aucune fenêtre en perte, ≥ moitié en gain | ❌ 3 en gain, 2 en perte | ❌ 3 en gain, 2 en perte | ❌ 3 en gain, 3 en perte | ❌ 1 en gain, 4 en perte |
| Ratio gains/pertes ≥ 1,3 | ✅ 2,29 | ✅ 2,21 | ❌ 1,17 | ❌ 0,70 |
| Perte maximale < 15 % | ✅ 5,7 % | ✅ 5,6 % | ✅ 12,9 % | ✅ 6,4 % |
| Sharpe dégonflé ≥ 0,95 | ❌ 0,43 | ❌ 0,38 | ❌ 0,00 | ❌ 0,00 |
| **Total** | **4/6** | **4/6** | **3/6** | **2/6** |
| Rendement moyen 5 ans | +5,0 % | +5,4 % | +4,6 % | −2,3 % |

Rendement par fenêtre (moyenne des 8 cryptos) :

| | F1 | F2 | F3 | F4 | F5 | F6 |
|---|---|---|---|---|---|---|
| volume_expansion 1 j | 0,00 % | −0,68 % | +2,33 % | +4,10 % | −0,92 % | +0,23 % |
| volume_chandelier | 0,00 % | −0,34 % | +4,05 % | +2,40 % | −1,04 % | +0,30 % |
| volume_expansion 4 h | −1,24 % | −0,44 % | +3,69 % | +0,71 % | +2,48 % | −0,76 % |
| rsi2_rebound | 0,00 % | +0,19 % | −0,66 % | −1,29 % | −0,07 % | −0,45 % |

### Lecture
- **Piste 1 écartée.** La sortie suiveuse relève le gain moyen (+11,20 € contre +9,23 €) et
  le rendement (+5,4 %), comme prévu, mais F2 et F5 restent en perte (F5 empire légèrement)
  et le Sharpe dégonflé baisse (0,38). Les pertes de ces fenêtres viennent des stops
  initiaux, que la façon de sortir des gagnants ne touche pas.
- **Piste 2 écartée.** En 4 h, le motif donne beaucoup de trades (891), mais une espérance
  faible (+1,24 €/trade), un ratio gains/pertes de 1,17 et trois fenêtres en perte : à
  cet horizon, le signal est trop proche du bruit une fois les frais payés. L'hypothèse
  « plus de trades = verdict plus favorable » est réfutée.
- **Piste 3 écartée.** Le rebond RSI 2 gagne une fois sur deux mais perd en moyenne
  (−0,93 €/trade, ratio 0,70) : les petits gains ne couvrent pas les stops et les frais.
  La règle publiée pour les actions ne se transpose pas à ce panier de cryptos.
- **Effet sur la barre.** Le Sharpe dégonflé de `volume_expansion` passe de 0,64 à 0,43
  (sa performance n'a pas changé) : trois essais de plus, et surtout un essai très négatif
  (RSI 2), élargissent l'écart entre les essais, donc le meilleur Sharpe attendu par hasard
  (0,038 → 0,051 par jour). C'est le coût voulu de la recherche : chaque idée testée rend
  plus difficile de prouver qu'un résultat n'est pas de la chance.

Statut : les pistes 1 et 3 restent « candidate », backtest seulement ; la piste 2 ne change
rien à `volume_expansion`. Pas de nouvel essai sur ces règles.
