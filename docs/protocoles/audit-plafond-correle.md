# Audit du plafond corrélé dans le portefeuille partagé (2026-11-22)

Demandé par la relecture croisée. Audit de cet historique, hors ligne : aucun changement du
moteur de risque ni de l'essai. volume_expansion, panier officiel, 5 ans, journalier.
Script : `audit_plafond_correle.py` ; résultats : `audit-plafond-correle-resultats.json`.

## 1. Les 58 interventions, une par une (rejeu de référence)
- 52 refus par le plafond : tous auraient été acceptés sans lui (aucun autre motif ne les aurait bloqués).
- 6 réductions de taille (quantité accordée entre 30 % et 82 % de la quantité demandée),
  dont 4 en juillet-août 2025.
- Autre motif de refus : nombre maximal de positions (8 fois).

## 2. Quatre rejeux complets (mêmes données, frais, exécution et ordre de traitement)

| Base de taille | Plafond | Rendement | Perte max. | Trades | €/trade | F2 | F5 |
|---|---|---|---|---|---|---|---|
| Capital courant (aujourd'hui) | en vigueur | **+40,6 %** | 13,9 % | 95 | +12,83 | −3,8 % | −4,4 % |
| Capital courant | retiré | +23,9 % | 13,9 % | 116 | +6,17 | −5,0 % | −5,9 % |
| Capital initial 3 000 € | en vigueur | +38,4 % | 11,5 % | 96 | +12,00 | −3,8 % | −3,3 % |
| Capital initial 3 000 € | retiré | +24,0 % | 11,9 % | 116 | +6,21 | −5,1 % | −4,8 % |

Approximation déclarée pour « capital initial » : quantité = minimum de la quantité permise
par le plafond et de celle que donnent 0,5 % de 3 000 €.

## Lecture
- Sur cet historique, le plafond corrélé n'a pas « coûté » : sans lui, le rendement tombe de
  +40,6 % à +23,9 %, le gain par trade est divisé par deux et les fenêtres 2 et 5 perdent davantage.
- Mécanisme visible : sans plafond, des positions corrélées occupent les places et le cash
  (refus « nombre maximal de positions » : 8 → 29), ce qui écarte des entrées ultérieures.
- L'effet du plafond est du même ordre quelle que soit la base de taille (+16,7 points en
  capital courant, +14,4 points en capital initial) : les deux mécanismes interagissent peu ici.
- La croissance du capital pèse peu sur le rendement (+40,6 % contre +38,4 %) mais augmente la
  perte maximale (13,9 % contre 11,5 %).
- Réponse au « 36 trades de moins » : 21 viennent du plafond (116 → 95 trades), le reste du
  passage à un seul capital (places et cash partagés).

Ceci explique cet historique ; ce n'est pas, à lui seul, une raison de changer le garde-fou.
Aucune variante n'est adoptée, research_trials inchangé (36).

## Clôture (2026-11-22)
Audit clos. Le plafond ne « repère » pas les mauvais trades : ses 52 refus préservent des
places et du cash pour les décisions suivantes ; les 6 réductions jouent un rôle secondaire.
Le dimensionnement sur le capital courant augmente surtout la perte maximale observée
(13,9 % contre 11,5 %) : constat sur ce rejeu, pas une raison de retoucher les tailles.
Aucun changement de Jarvis. Recherche de stratégies et filtres en pause ; priorité à la
première démo hors ligne.
