# Deux pistes autour de volume_expansion, figées AVANT tout code et tout backtest

Rédigé le 2026-11-24 par Claude Code. Ce fichier est commité avant la première ligne de code
de ces pistes et avant leur évaluation : le commit en fait foi. Tag de restauration :
`avant-pistes-20261124`. Aucun seuil officiel n'est changé ; aucun paramètre ne sera retouché
après coup ; un échec est noté tel quel.

## Point de départ
- `volume_expansion` : 4/6 à l'évaluation officielle du 2026-11-22 (131 trades, +9,23 €/trade,
  ratio gains/pertes ≥ 1,3, perte max < 15 %), mais 2 fenêtres en perte (F2 −0,7 %, F5 −0,9 %)
  et Sharpe dégonflé 0,65 (36 essais).
- Le diagnostic des fenêtres (`diagnostic-fenetres.md`) montre que F2 et F5 sont des grappes
  de stops pleins tombés les mêmes jours sur plusieurs cryptos (2023-06-10, 2025-09-22,
  2025-10-10) : un choc de marché commun, pas un défaut d'une crypto isolée.
- Limite assumée : ces pistes sont pensées après avoir vu ce diagnostic, sur les mêmes
  données. Ce n'est pas un test hors échantillon ; c'est précisément pour cela que chaque
  piste compte comme un essai dans le Sharpe dégonflé.

## Ce qu'on n'attend PAS
Aucune stratégie ne « gagne toujours ». Le but est une espérance positive qui résiste aux six
critères. Chaque essai supplémentaire relève la barre du Sharpe dégonflé pour toutes les
stratégies ; on se limite donc à deux pistes.

## Piste A — Expansion de volume avec largeur du marché (`volume_breadth`)
- **Hypothèse** : les stops groupés de F2 et F5 frappent surtout des achats faits quand le
  mouvement n'était porté que par quelques cryptos. Exiger qu'une majorité du panier soit
  en tendance au jour du signal doit retirer en priorité ces achats exposés aux chocs
  communs, sans toucher au moteur de l'entrée.
- **Règle** : signal de `volume_expansion` inchangé (volume ≥ 2 × moyenne 20, bougie haussière
  clôturant dans son tiers haut, clôture > SMA(100), stop = clôture − 2 × ATR(14)), ET au jour J
  au moins 5 des 8 cryptos du panier ont leur clôture strictement au-dessus de leur SMA(100).
  Si la SMA(100) n'est pas calculable pour les 8 ce jour-là : pas d'entrée.
- Sortie inchangée : clôture sous l'EMA(20), exécutée à l'ouverture suivante ; stop dans la bougie.
- **Prédiction honnête** : moins de trades (risque de passer sous 100), fenêtres F2/F5 moins
  négatives. Si le filtre retire autant de bons trades que de mauvais, rien ne change.

## Piste B — Expansion de volume sur les cryptos meneuses (`volume_leaders`)
- **Hypothèse** : une poussée de volume sur une crypto déjà parmi les plus fortes du panier
  prolonge plus souvent le mouvement (le volume confirme un momentum existant) ; sur une
  crypto à la traîne, c'est plus souvent un sursaut sans suite qui finit au stop.
- **Règle** : signal de `volume_expansion` inchangé, ET au jour J la crypto est dans la
  moitié haute du panier (rang 1 à 4 sur 8) selon le rendement sur 90 jours (même classement
  que la candidate `relative_strength` : toutes les paires calculables, égalités par ordre
  alphabétique). Classement incomplet : pas d'entrée.
- Sortie inchangée (EMA(20), stop 2 × ATR(14)) ; le rang n'intervient pas dans la sortie.
- **Prédiction honnête** : moins de trades (même risque sous 100), espérance par trade plus haute.

## Règles d'arrêt (identiques pour les deux pistes)
- Une seule évaluation officielle par piste : 8 actifs (BTC, ETH, SOL, XRP, ADA, LINK, DOGE,
  LTC, EUR, Bitvavo), journalier, 1825 jours, 6 fenêtres, filtre de régime et veto Markov
  selon les réglages en vigueur, terrain de régime identique à `volume_expansion`
  (haussier et sans tendance, volatilité haute permise).
- **Validée** seulement si les 6 critères passent. Elle reste alors « candidate », backtest
  seulement ; tout passage au papier demande un protocole séparé et une décision humaine.
- **Écartée** dès qu'un critère échoue, avec ses chiffres. Pas de variante (4 ou 6 cryptos
  sur 8, rang 1 à 3, autre moyenne…), pas de nouvel essai sur ces règles.
- `research_trials` passe de 36 à 38 avant l'évaluation.
- Les deux pistes sont marquées « backtest seulement » : refusées comme stratégie de l'essai,
  absentes du laboratoire. Résultats écrits dans `data/evaluations-pistes-20261124/`, pas
  dans `data/evaluations/` lue par l'essai.

## Résultats (évaluation officielle du 2026-11-24, 8 actifs, journalier, 1825 jours, 38 essais)

Aucune des deux pistes ne passe. Aucun paramètre n'a été retouché ; aucun seuil changé.
Toutes les stratégies ont été réévaluées dans le même passage (fichiers dans
`data/evaluations-pistes-20261124/`, hors de `data/evaluations/`).

| Critère | volume_expansion (référence) | volume_breadth (A) | volume_leaders (B) |
|---|---|---|---|
| ≥ 100 trades clôturés | ✅ 131 | ✅ 111 | ❌ 88 |
| Espérance nette > 0 | ✅ +9,23 €/trade | ✅ +7,43 €/trade | ✅ +8,89 €/trade |
| Aucune fenêtre en perte, ≥ moitié en gain | ❌ 3 en gain, 2 en perte | ❌ 2 en gain, 3 en perte | ❌ 3 en gain, 2 en perte |
| Ratio gains/pertes ≥ 1,3 | ✅ 2,29 | ✅ 1,96 | ✅ 2,40 |
| Perte maximale < 15 % | ✅ 5,7 % | ✅ 5,7 % | ✅ 4,4 % |
| Sharpe dégonflé ≥ 0,95 | ❌ 0,64 | ❌ 0,45 | ❌ 0,64 |
| **Total** | **4/6** | **4/6** | **3/6** |
| Rendement moyen 5 ans | +5,0 % | +3,4 % | +3,3 % |

Rendement par fenêtre (moyenne des 8 cryptos) :

| | F1 | F2 | F3 | F4 | F5 | F6 |
|---|---|---|---|---|---|---|
| volume_expansion | 0,00 % | −0,68 % | +2,33 % | +4,10 % | −0,92 % | +0,23 % |
| volume_breadth | 0,00 % | −0,69 % | +1,67 % | +3,49 % | −0,89 % | −0,09 % |
| volume_leaders | 0,00 % | −0,34 % | +1,88 % | +1,84 % | −0,47 % | +0,35 % |

### Lecture
- **Piste A écartée.** Le filtre de largeur retire 20 trades, mais surtout des gagnants :
  F2 et F5 restent en perte presque à l'identique, F6 bascule en perte, le Sharpe dégonflé
  baisse (0,45). L'hypothèse « les stops groupés tombent dans un marché étroit » est réfutée
  sur ces données : les chocs de 2023-06 et 2025-10 ont frappé un marché large.
- **Piste B écartée.** Le filtre des meneuses divise par deux les pertes de F2 et F5 et garde
  une bonne espérance (+8,89 €/trade, ratio 2,40), ce qui va dans le sens de l'hypothèse ;
  mais il tombe à 88 trades, les deux fenêtres restent en perte et le Sharpe dégonflé ne
  bouge pas (0,64). Conformément à la règle d'arrêt, pas de variante (rang 1 à 5, etc.).
- Référence : volume_expansion passe de 0,65 à 0,64 de Sharpe dégonflé du seul fait des deux
  essais en plus. C'est le prix, voulu, de chaque idée testée.

Statut : les deux pistes restent « candidate », backtest seulement. Pas de nouvel essai sur
ces règles.
