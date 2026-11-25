# Diagnostic des fenêtres de l'évaluation officielle (2026-11-22)

Demandé par la relecture croisée (ChatGPT « Luna »). Lecture seule : l'évaluation officielle,
ses critères et ses résultats ne sont pas modifiés. Script : `diagnostic_fenetres.py`.
Panier officiel (8 cryptos, journalier), début officiel le 2021-11-24, 6 fenêtres d'environ 304 jours.

## 1. Fenêtre 1 : l'historique manquant n'est PAS l'explication principale

Rejeu en donnant aux indicateurs 400 jours d'historique antérieur, sans aucune entrée ni
performance comptée avant le 2021-11-24. Données disponibles depuis le 2020-10-20, sauf
SOL (2021-08-03) et DOGE (2021-08-05) : pour ces deux-là, l'initialisation reste partielle.

| Stratégie | F1 officielle | F1 avec historique antérieur |
|---|---|---|
| volume_expansion | 0 trade, 0,0 % | 1 trade, −0,0 % (−8 €) |
| relative_strength | 0 trade, 0,0 % | 12 trades, −0,5 % (−114 €) |
| compression_breakout | 0 trade, 0,0 % | 0 trade, 0,0 % |
| dip_rebound | 0 trade, 0,0 % | 1 trade, −0,0 % (−9 €) |

Fenêtres 2 à 6 : identiques à quelques euros près. Conclusion : la fenêtre 1
(nov. 2021 → sept. 2022) est le marché baissier de 2022. Le filtre de régime y bloque
les entrées en tendance baissière (premier motif de veto de volume_expansion et de
relative_strength). Donner plus d'historique n'y crée pas de gains ; pour la force
relative, cela ajoute même des pertes. On cesse d'invoquer le manque d'historique.

## 2. Frontières de fenêtres : pas d'anomalie

La simulation est continue (une seule par crypto) ; le rendement d'une fenêtre se mesure
sur la valeur du portefeuille aux frontières, positions ouvertes au prix du marché ; un
trade est compté dans la fenêtre de sa vente.
Exemple suivi : volume_expansion, SOL/EUR, achat le 2023-07-08 à 19,6286 € (frais 0,28 €),
vente le 2023-07-25 à 21,0949 € (frais 0,30 €), résultat +7,81 €, vente sur signal, frontière
au 2023-07-25. La hausse latente avant la frontière compte en F2, le reste en F3 ; le trade
est compté en F3. Convention cohérente, conservée.

## 3. Tableau stratégie × fenêtre (évaluation officielle)

Rendement moyen par crypto · trades vendus · résultat net · frais.

| | F1 nov. 21→sept. 22 | F2 sept. 22→juil. 23 | F3 juil. 23→mai 24 | F4 mai 24→mars 25 | F5 mars 25→janv. 26 | F6 janv. 26→nov. 26 |
|---|---|---|---|---|---|---|
| volume_expansion | 0,0 % · 0 | −0,7 % · 29 · −166 € · 26 € | +2,3 % · 36 · +561 € | +4,1 % · 22 · +993 € | −0,9 % · 26 · −232 € · 22 € | +0,2 % · 18 · +53 € |
| relative_strength | 0,0 % · 0 | −0,2 % · 25 · −49 € · 16 € | +3,1 % · 36 · +721 € | +0,2 % · 34 · +73 € | −0,0 % · 19 · −6 € · 11 € | +0,1 % · 18 · +22 € |
| compression_breakout | 0,0 % · 0 | −0,3 % · 9 · −64 € · 10 € | +0,7 % · 13 · +170 € | −0,1 % · 3 · −22 € | −0,1 % · 9 · −34 € · 12 € | +0,0 % · 1 · +10 € |
| dip_rebound | 0,0 % · 0 | −0,2 % · 6 · −38 € · 4 € | −0,0 % · 17 · −10 € | +0,2 % · 6 · +49 € | +0,1 % · 7 · +19 € | −0,1 % · 2 · −12 € |

## Trois histoires différentes, pas une seule « irrégularité »

1. **F1 : marché baissier filtré.** Aucun trade, par choix de prudence fixé à l'avance. Une
   fenêtre à 0 % n'est pas en perte, mais elle ne compte pas comme gagnante.
2. **F2 et F5 : grappes de stops, aux mêmes dates pour plusieurs stratégies.** Les pires
   pertes sont presque toutes des stops pleins d'environ −15 à −17 € (le risque maximal de
   0,5 % par trade), et elles tombent les mêmes jours : 2023-06-10 (BTC, LTC, XRP), 2025-09-22
   (DOGE), 2025-10-10 (SOL, LINK, BTC). Les stratégies sont donc moins diversifiées qu'elles
   n'en ont l'air : un même choc de marché les touche ensemble. Les frais ne suffisent pas à
   expliquer ces fenêtres (volume_expansion F2 : −166 € de résultat, dont 26 € de frais).
3. **Pertes faibles en taille.** Les fenêtres « en perte » sont entre −0,1 % et −0,9 %.
   Aucune ne vient d'un seul trade : ce sont des séries de stops.

Aucune modification de règle, de seuil ou de convention n'est faite sur la base de ce diagnostic.
