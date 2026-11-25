# Jarvis : 18 agents IA qui tradent la crypto en argent fictif

*An autonomous multi-agent crypto trading research system, paper trading only. Docs in French.*

**[Voir la démo en 35 secondes](https://charly4perrin-jpg.github.io/jarvis-trading/)** : les agents refusent, achètent, coupent une perte et prennent un gain (argent fictif). L'essai en direct, mis à jour toutes les heures, est sur [live.html](https://charly4perrin-jpg.github.io/jarvis-trading/live.html).

> ⚠️ Argent fictif uniquement, aucun ordre réel. Ce n'est pas un conseil en investissement. Lire [AVERTISSEMENT.md](AVERTISSEMENT.md).

## L'idée

Un seul « bot » qui décide tout seul, c'est fragile. Jarvis répartit la décision entre 18 agents spécialisés, avec chacun un rôle et des droits limités. Plusieurs agents sont là pour **dire non**.

```
Scanner → Marché → Régime → Stratégie → Contradicteur → Proposition → Allocation
        → Risque (règles fixes, sans IA) → Routage → Exécution simulée → Portefeuille → Audit
```

Un refus, à n'importe quelle étape, bloque définitivement la proposition. Quand une vérification est impossible, rien ne part (*fail-closed*).

## Ce qu'il y a dedans

- **18 agents** sur un bus d'événements chaîné (SHA-256) : chaque décision est reconstituable et le journal est infalsifiable.
- **Risque :** 0,5 % par trade, pertes maximales de 2 % par jour et 5 % par semaine, réserve de cash, corrélations, vitesse de perte, arrêt d'urgence conservé après redémarrage.
- **Simulation réaliste** sur le vrai carnet d'ordres (Bitvavo, Kraken, via CCXT) : frais, glissement, exécutions partielles, ordres limite.
- **Laboratoire :** 8 stratégies en compétition, évaluation walk-forward, critères d'acceptation fixés à l'avance, Sharpe dégonflé et PBO contre le sur-apprentissage.
- **Mode Shadow :** chaque signal refusé est suivi, pour mesurer ce que les protections évitent ou font manquer.
- **R&D autonome :** chaque nuit, des agents Claude Code proposent, codent, testent et critiquent des améliorations. Un garde-fou sans IA leur interdit de toucher au risque, à la sécurité, à l'audit et à la configuration.
- **Interface locale** de 14 espaces, avec un personnage par agent et le fil de décision sous forme de conversation.
- Plus de 180 tests (dont des tests de propriétés) et 10 scénarios extrêmes.

## Résultats honnêtes

Sur 5 ans de bougies journalières, 6 stratégies sur 8 gagnent un peu par trade, mais **aucune ne passe les critères d'acceptation**. En bougies de 4 h, les frais mangent tout. L'arbitrage Bitvavo/Kraken rapporte environ 0,5 point de base d'écart, contre environ 51 de frais aller-retour. Jarvis sert d'abord à apprendre à ne pas se raconter d'histoires.

## Démarrer

**Sur Mac, sans ligne de commande :** téléchargez le projet (bouton vert **Code** → **Download ZIP**), décompressez-le, puis double-cliquez sur `Lancer Jarvis.command`. Pas à pas : [docs/installation.md](docs/installation.md). Pour rejouer la démo sur votre machine : `python -m trading_agent demo` ([docs/demo.md](docs/demo.md)).

Pour les développeurs :

```bash
git clone https://github.com/charly4perrin-jpg/jarvis-trading.git && cd jarvis-trading
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env              # argent fictif par défaut, aucune clé requise
python -m pytest -q               # la suite de tests
python -m trading_agent ui        # interface : http://127.0.0.1:8765
```

Commandes utiles : `verify` (intégrité du journal), `scenarios` (scénarios extrêmes), `status`, `halt` / `resume`, `backtest`, `overfitting`. Détails techniques dans [docs/README_TECHNIQUE.md](docs/README_TECHNIQUE.md).

Les clés d'IA (Grok ou ChatGPT) sont facultatives : elles servent uniquement à donner un avis en langage clair, jamais à décider.

## Licence

[Apache 2.0](LICENSE). Construit par Charly Perrin, étudiant en Business & Engineering à Maastricht. Suivi du projet chaque semaine sur LinkedIn. Contributions et critiques bienvenues.
