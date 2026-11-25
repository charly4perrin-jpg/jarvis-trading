# Crypto Trading Agent — exchange comparison + paper trading

Version 0.2.0. This package supports **public market data only** from Bitvavo
and Kraken through CCXT, plus a local paper broker. It contains no API-key
handling and cannot place live orders.

## 0. Essayer en 5 minutes (sans compte, sans clé, argent fictif)

Jarvis est un **outil de suivi de portefeuille et un laboratoire de trading crypto
simulé**. Ce n'est pas un robot rentable : aucune stratégie actuelle ne passe les six
critères de validation, et aucun ordre réel n'est possible.

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m trading_agent demo        # un refus, un achat papier, un stop, avec les motifs
.venv/bin/python -m trading_agent holdings set --exchange bitvavo --asset BTC --quantity 0.01
.venv/bin/python -m trading_agent holdings show   # valeur de vos soldes, cours publics en euros
```

La démo fonctionne hors ligne et sans `.env` (voir `docs/demo.md`). Le suivi de
portefeuille ne connecte aucun compte (voir `docs/portefeuille.md`).

Sur Mac, sans ligne de commande : double-cliquez sur « Lancer Jarvis » (voir `docs/installation.md`).

## 1. Install
```bash
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

## 2. Compare exchanges (public endpoints)
```bash
python -m trading_agent compare --quote EUR --min-quote-volume 50000 --limit 30
```
This loads Spot markets and public tickers from Bitvavo and Kraken, then
compares active EUR-quoted markets by ticker availability, reported 24h quote
volume, bid/ask spread and market metadata. Missing volume fields are treated
as unknown, not as zero-quality data.

## 3. Run paper trading data loop
```bash
python -m trading_agent paper --exchange bitvavo --symbol BTC/EUR --timeframe 5m
python -m trading_agent paper --exchange kraken --symbol BTC/EUR --timeframe 5m
```
The loop fetches public OHLCV candles, evaluates a deliberately simple
demonstration momentum rule, and writes hypothetical decisions/fills to
`data/paper_ledger.jsonl`. The strategy is educational and is not validated
for profitability. No live orders are sent.

## 4. Design boundaries
- `exchange` adapter: public endpoints only (`load_markets`, `fetch_tickers`,
  `fetch_ohlcv`, `fetch_order_book`).
- `paper` broker: simulated fills with configurable fee and slippage.
- `risk` module: blocks trades when daily loss, weekly loss, exposure, cash
  reserve or open-position limits are breached.
- `audit` module: JSONL event log.
- There are no credentials, private endpoints, deposit/withdrawal methods,
  or live order methods in this version.

## 5. Before any live-trading milestone
1. Confirm the chosen provider's availability and service scope for your
   residence, and review the applicable AFM/EU register and provider terms.
2. Run a multi-week paper-trading trial across different market regimes.
3. Include realistic maker/taker fees, spread, slippage, latency, partial
   fills, rejected orders and outages in the simulator.
4. Test stop/exit handling and restart/reconciliation logic.
5. Review security and operational controls independently.
6. Only then design a separate, disabled-by-default live execution adapter.

## 6. Système multi-agents et interface Jarvis
```bash
python -m trading_agent ui --exchange bitvavo --symbol BTC/EUR      # interface : http://127.0.0.1:8765
python -m trading_agent agents --exchange bitvavo --symbol BTC/EUR  # un cycle dans le terminal
python -m trading_agent trace                                       # réaffiche le dernier fil de décision
python -m trading_agent verify                                      # vérifie que le journal est intact
python -m trading_agent backtest --timeframe 1h --days 180          # rejoue la stratégie sur l'historique
python -m trading_agent halt --reason "motif"                       # arrêt d'urgence (puis `resume`)
```
Tout reste en paper trading : données publiques, argent fictif, aucun ordre réel.
