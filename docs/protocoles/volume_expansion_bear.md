# Protocole fixé à l'avance : volume_expansion_bear contre volume_expansion (laboratoire)

Rédigé le 2026-11-22, AVANT toute lecture de performance du laboratoire pour ces deux stratégies.
Proposé par une relecture croisée (ChatGPT « Luna »), appliqué par Claude Code. Ne pas modifier
ce fichier après le début de l'observation, sauf pour y ajouter les bilans en bas.

## Appariement (vérifié le 2026-11-22 à 16 h UTC)
- Témoin : `volume_expansion` dans le laboratoire journalier (`data/lab_1d`).
- Variante : `volume_expansion_bear`, mêmes règles, seule différence : achats permis en tendance baissière.
- État initial identique : 3 000 € de cash, aucune position, aucun ordre en attente, aucun trade, pour les deux.
- Mêmes données : le laboratoire passe la même bougie close de chaque paire suivie à toutes les stratégies, au même moment.
- Début de la comparaison : **2026-11-22**. Ce qui précède ne compte pas (le témoin n'avait rien fait avant).
- L'essai principal (autre date de départ, carnet réel) ne sert PAS de témoin.

## Mesure et règle de décision (identiques au test hors échantillon)
- Mesure principale : rendement net cumulé de la variante moins celui du témoin, en points.
- Intervalle à 95 % : rééchantillonnage des rendements journaliers par blocs de 28 jours, mêmes blocs pour les deux.
- Adopter pour l'essai principal (après validation humaine) seulement si : au moins 40 trades supplémentaires clôturés issus de signaux bloqués par le veto, au moins +1 point, borne basse de l'intervalle strictement positive, ET la variante passe les 6 critères officiels.
- Abandonner si la borne haute est ≤ 0.
- Sinon : non concluant, on garde le veto et on continue d'observer, sans changer la période ni les paires.

## Dates de bilan (fixées maintenant)
- Premier bilan : **2027-05-22**.
- Bilan suivant si non concluant : **2027-11-22**.
- Aucune conclusion entre deux bilans, même si l'écart paraît convaincant.

## Bilans
(à compléter aux dates ci-dessus)
