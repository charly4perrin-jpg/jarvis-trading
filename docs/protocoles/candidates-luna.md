# Trois stratégies candidates, règles figées AVANT tout backtest

Proposées par une relecture croisée (ChatGPT « Luna ») le 2026-11-22, recopiées ici sans
modification de fond, avec les seules interprétations nécessaires pour les coder (section
« Précisions »). Ce fichier est commité avant la première ligne de code et avant tout
backtest de ces candidates : le commit en fait foi. Aucun paramètre ne sera retouché après
coup. Un échec est noté tel quel ; aucun seuil officiel n'est changé pour faire passer une
candidate. Chaque candidate compte comme une configuration essayée (`research_trials`).

## Règles communes
- Bougies journalières UTC clôturées seulement. J = dernière bougie close ; une fenêtre
  « précédente » exclut J.
- SMA(n) = moyenne simple des n dernières clôtures, J comprise.
- ATR(14) = moyenne simple des 14 dernières amplitudes vraies, J comprise ; amplitude vraie
  = max(haut − bas, |haut − clôture précédente|, |bas − clôture précédente|).
- Donnée nécessaire manquante, non finie, ou ATR nul : pas d'entrée. Pas de renfort.
- Vetos existants, dimensionnement et routage inchangés.
- P = prix de référence de la proposition (clôture de J). Stop initial = P − distance.
  Objectif = P + 2 × distance (convention de Jarvis, informatif : le courtier papier
  n'exécute pas d'objectif). Pas de stop suiveur. Niveaux invalides : pas d'achat.
- Sorties sur signal journalier : jamais exécutées à la clôture qui a produit le signal
  (règle des backtests : ouverture suivante).

## Candidate 1 — Force relative à trois mois (`relative_strength`)
- Rendement R = clôture J / clôture J−90 − 1, pour les huit actifs officiels.
- Entrée si l'actif est parmi les trois premiers selon R, si R > 0 strictement et si
  clôture J > SMA(100). Égalités départagées par ordre alphabétique du symbole.
  Si les R des huit actifs ne sont pas tous calculables : aucune nouvelle entrée ce jour-là.
- Sortie si le rang tombe à 5 ou moins bon, ou si clôture J < SMA(100). Un classement
  indisponible ne déclenche pas de vente à lui seul ; la sortie par la moyenne reste
  active si elle est calculable.
- Distance au stop : 2,5 × ATR(14).

## Candidate 2 — Cassure après compression (`compression_breakout`)
- Sur les 10 bougies précédentes (J−10 à J−1) : B = plus haut des hauts, A = plus bas des bas.
- Entrée si (B − A) / clôture(J−1) ≤ 8 %, si clôture J > B strictement, et si clôture J > SMA(100).
- Sortie si une clôture repasse strictement sous B (B mémorisé au signal d'entrée), ou au
  10e jour UTC suivant le premier fill, au premier cycle journalier de ce jour.
- Distance au stop : 2 × ATR(14).

## Candidate 3 — Rebond après une chute, en tendance positive (`dip_rebound`)
- Chute en J−1 : clôture(J−1) / clôture(J−2) − 1 ≤ −5 %.
- Entrée en J si clôture J > (haut(J−1) + bas(J−1)) / 2, si clôture J > SMA(100), et si
  bas(J) ≥ bas(J−1).
- Sortie si une clôture passe strictement sous bas(J−1) mémorisé à l'entrée, ou au 5e jour
  UTC suivant le premier fill, au premier cycle journalier de ce jour.
- Distance au stop : 2 × ATR(14).

## Précisions d'implémentation (fixées maintenant, pas après les résultats)
- « Huit actifs officiels » = le panier de l'évaluation officielle : BTC, ETH, SOL, XRP,
  ADA, LINK, DOGE, LTC (EUR, Bitvavo). Les candidates sont évaluées en backtest seulement ;
  elles ne pilotent ni l'essai principal ni le laboratoire.
- Rang : 1 = meilleur R. « Rang 5 ou moins bon » = rang ≥ 5.
- Sortie au N-ième jour UTC après le premier fill : le fill a lieu à l'ouverture du jour F ;
  la vente a lieu à l'ouverture du jour F + N, donc le signal de sortie est levé à la clôture
  du jour F + N − 1 (cohérent avec la règle « ouverture suivante »).
- Les niveaux mémorisés (B, bas(J−1)) sont ceux de la bougie du signal d'entrée.
- Stop : géré par le courtier papier comme pour les autres stratégies (vérifié dans la
  bougie, exécuté à l'ouverture si elle est déjà sous le stop).
- Protocole d'évaluation : officiel et inchangé (8 actifs, journalier, 1825 jours, six
  critères, filtre de régime et veto Markov selon les réglages en vigueur).

## Précisions ajoutées avant tout backtest (même jour, code écrit, aucun résultat consulté)
- Terrain de régime des trois candidates : celui par défaut de Jarvis (tendances haussière
  et sans tendance, volatilité haute exclue), comme la plupart des stratégies existantes ;
  veto Markov selon le réglage en vigueur. Aucun réglage propre aux candidates.
- Les trois candidates sont marquées « backtest seulement » : refusées comme stratégie de
  l'essai, absentes du laboratoire. La force relative n'entre jamais sans panier lié.
- `research_trials` passe à 36 (33 + 3 candidates) avant l'évaluation.

## Résultats (évaluation officielle du 2026-11-22, 8 actifs, journalier, 1825 jours, 36 essais)

Aucune des trois candidates ne passe. Aucun paramètre n'a été retouché ; aucun seuil changé.

| Critère | relative_strength | compression_breakout | dip_rebound |
|---|---|---|---|
| ≥ 100 trades clôturés | ✅ 132 | ❌ 35 | ❌ 38 |
| Espérance nette > 0 | ✅ +5,76 €/trade | ✅ +1,70 €/trade | ✅ +0,21 €/trade |
| Aucune fenêtre en perte, ≥ moitié en gain | ❌ 3 en gain, 2 en perte | ❌ 2 en gain, 3 en perte | ❌ 2 en gain, 3 en perte |
| Ratio gains/pertes ≥ 1,3 | ✅ 1,97 | ❌ 1,25 | ❌ 1,05 |
| Perte maximale < 15 % | ✅ 10,6 % | ✅ 1,5 % | ✅ 1,1 % |
| Sharpe dégonflé ≥ 0,95 | ❌ 0,33 | ❌ 0,19 | ❌ 0,04 |
| **Total** | **4/6** | **2/6** | **2/6** |
| Rendement total 5 ans | +3,2 % | +0,2 % | +0,0 % |

Référence, même évaluation : volume_expansion 4/6 (+9,23 €/trade, Sharpe dégonflé 0,65).

Incident de mise en œuvre, corrigé avant publication : la première évaluation (même jour)
calculait le Sharpe dégonflé sur un backtest de portefeuille où la force relative n'était
pas liée au panier et où les sorties mémorisées n'étaient pas branchées (commit 656ce7f).
Les cinq autres critères n'étaient pas concernés et sont identiques dans les deux passages.
Résultats du premier passage conservés dans `data/evaluations-candidates-defaut/`.

Statut : les trois candidates restent « candidate », backtest seulement. Pas de nouvel
essai de variantes de ces règles.
