# Relevé du risque ouvert, jour par jour (2026-11-22)

Demandé par la relecture croisée. Lecture seule. Un seul portefeuille réel : backtest à
capital partagé de volume_expansion sur le panier officiel, 1 823 jours (+40,6 %, perte
maximale 13,9 %, 95 trades). Règles de classement fixées avant lecture (voir le script).

| Jours | Nombre | Jours avec stop | Stops | Perte des stops | Grappes (≥ 2 stops) |
|---|---|---|---|---|---|
| Baisse commune, risque ouvert élevé | 14 | 4 | 5 | −76,89 € | 1 |
| Baisse commune, risque ouvert faible | 78 | 6 | 7 | −135,36 € | 1 |
| Pas de baisse, risque ouvert élevé | 273 | 4 | 4 | −68,72 € | 0 |
| Tous les jours | 1 823 | 24 | 26 | −457,76 € | 2 |

Seuils : baisse commune = panier ≤ −5,55 % sur la journée (5 % des pires jours) ; risque
ouvert élevé = au moins 54,63 € jusqu'aux stops (médiane des jours avec position).

Les deux seules grappes en cinq ans, dans le vrai portefeuille :
- 2022-11-08 (panier −14,5 %, BTC −10,3 %) : LINK et LTC stoppés, −30,6 € ; risque ouvert 84,97 €.
- 2025-10-10 (panier −13,3 %, BTC −6,4 %) : BTC et LTC stoppés, −42,2 € ; risque ouvert 35,59 €.

Plafond corrélé existant (1 % du capital) : sur 155 propositions approuvables, il en a
refusé 52 et réduit 6. Il agit donc déjà fortement.

## Conclusion
- Les « grappes » du diagnostic précédent venaient en partie de portefeuilles fictifs
  indépendants (un par crypto) : dans le vrai portefeuille partagé, il n'y en a que deux en
  cinq ans, lors de deux krachs de tout le marché.
- Les pertes sur stop ne sont pas concentrées sur les jours à risque ouvert élevé : les baisses
  communes à risque faible coûtent davantage (78 jours, −135 €) que celles à risque élevé
  (14 jours, −77 €).
- Le plafond corrélé limite déjà le risque agrégé. Un plafond global n'aurait au mieux bloqué
  qu'une entrée avant la grappe de 2022 ; celle de 2025 est survenue avec un risque ouvert
  sous la médiane.
- Piste « abandonnée après diagnostic, sans variante testée » : le problème qu'un plafond global devait résoudre n'apparaît pas assez nettement pour justifier un essai (ce n'est pas une preuve de son inutilité) ; aucun essai lancé,
  research_trials inchangé (36).
