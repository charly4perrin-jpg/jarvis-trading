"""Actualités : incidents et maintenances publiés par les plateformes elles-mêmes.

Sources : pages d'état officielles de Bitvavo et Kraken (format JSON public, sans
clé). Ce sont des informations vérifiées : la plateforme les publie elle-même.

Aucune interprétation par une intelligence artificielle : le système n'utilise
aucun modèle de langage. Le champ `interpretation` reste vide et le dit.

Effet sur le trading : seul un incident officiel majeur ou critique, en cours, qui
touche le trading ou l'API d'une plateforme, suspend les nouvelles entrées sur cette
plateforme (via l'Exchange Monitor). Le reste est affiché pour information.

Le calendrier économique est à part (macro_calendar.py), en lecture seule.
"""
import asyncio
import json
import re
import urllib.request

STATUS_PAGES = {
    "bitvavo": "https://status.bitvavo.com/api/v2/summary.json",
    "kraken": "https://status.kraken.com/api/v2/summary.json",
}
TRADING_WORDS = ("trading", "order", "api", "websocket", "matching", "market data", "spot", "exchange")
NAMES = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp", "ADA": "cardano",
         "DOGE": "dogecoin", "LTC": "litecoin", "LINK": "chainlink"}
OPEN = {"investigating", "identified", "monitoring", "in_progress", "verifying", "scheduled"}


def fetch(url: str, timeout: float = 10) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "jarvis-paper-monitor"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def classify(exchange: str, data: dict, watched: list[str]) -> dict:
    bases = {s.split("/")[0] for s in watched}
    items = []
    for kind, key in (("incident", "incidents"), ("maintenance", "scheduled_maintenances")):
        for i in data.get(key) or []:
            name = i.get("name") or ""
            low = name.lower()
            assets = sorted(b for b in bases
                            if re.search(rf"\b{b.lower()}\b", low) or NAMES.get(b, "\0") in low)
            trading = any(re.search(rf"\b{w}\b", low) for w in TRADING_WORDS)
            ongoing = (i.get("status") or "") in OPEN and not (kind == "maintenance" and i.get("status") == "scheduled")
            blocking = ongoing and trading and i.get("impact") in ("major", "critical")
            items.append({
                "exchange": exchange, "kind": kind, "title": name, "status": i.get("status"),
                "impact": i.get("impact"), "updated_at": i.get("updated_at"),
                "url": i.get("shortlink") or (data.get("page") or {}).get("url"),
                "source": f"page d'état officielle {exchange}", "verified": True, "interpretation": None,
                "assets": assets, "touches_trading": trading, "blocking": blocking,
                "relevance": "bloquant" if blocking else "actif suivi" if assets
                             else "trading" if trading else "autre"})
    status = data.get("status") or {}
    return {"exchange": exchange, "indicator": status.get("indicator"), "description": status.get("description"),
            "items": items, "blocking": [i["title"] for i in items if i["blocking"]]}


async def collect(watchlists: dict[str, list[str]], fetcher=fetch) -> dict[str, dict]:
    out = {}
    for exchange, watched in watchlists.items():
        url = STATUS_PAGES.get(exchange)
        if not url:
            continue
        try:
            data = await asyncio.to_thread(fetcher, url)
            out[exchange] = classify(exchange, data, watched)
        except Exception as exc:  # une page d'état injoignable ne bloque rien
            out[exchange] = {"exchange": exchange, "error": f"{type(exc).__name__} {exc}"[:200],
                             "items": [], "blocking": []}
    return out
