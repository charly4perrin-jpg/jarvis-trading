"""Portefeuille réel, en lecture seule : ce que l'utilisateur détient vraiment.

Saisie à la main ou par CSV (plateforme, actif, quantité), valorisation avec les cours
publics en euros. Aucune clé API, aucun ordre, aucun lien avec le simulateur papier :
le fichier est séparé (data/holdings.json) et les 3 000 € fictifs ne sont jamais lus
ni modifiés ici.

Un relevé est une PHOTO des soldes : importer deux fois le même relevé remplace les
quantités au lieu de les additionner. Les dates sont affichées séparément :
« quantités au … » (date du relevé) et « prix au … » (date du cours). Un cours frais ne
veut pas dire que le solde est à jour.
"""
import asyncio
import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = "data/holdings.json"
MAX_PRICE_AGE_S = 15 * 60
UNAVAILABLE = "valorisation indisponible"
COLUMNS = {"plateforme": "exchange", "exchange": "exchange", "actif": "asset", "asset": "asset",
           "quantite": "quantity", "quantité": "quantity", "quantity": "quantity",
           "date": "as_of"}


def _guard(path: Path) -> Path:
    from trading_agent.demo_run import main_trial_paths
    resolved = path.resolve()
    if resolved in main_trial_paths():
        raise ValueError(f"Refusé : {path} est un fichier de l'essai papier, pas un relevé.")
    return path


def load(path: str | Path = DEFAULT_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        return {"lines": {}, "statements": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save(data: dict, path: str | Path = DEFAULT_PATH) -> None:
    path = _guard(Path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _key(exchange: str, asset: str) -> str:
    return f"{exchange.strip().lower()}:{asset.strip().upper()}"


def set_line(data: dict, exchange: str, asset: str, quantity: float, as_of: str, source: str) -> None:
    quantity = float(quantity)
    if not quantity >= 0 or quantity == float("inf"):
        raise ValueError(f"Quantité invalide pour {asset} sur {exchange} : {quantity}")
    key = _key(exchange, asset)
    if quantity == 0:
        data["lines"].pop(key, None)   # solde nul : la ligne n'est plus détenue
        return
    data["lines"][key] = {"exchange": exchange.strip().lower(), "asset": asset.strip().upper(),
                          "quantity": quantity, "as_of": as_of, "source": source}


def import_csv(csv_path: str | Path, path: str | Path = DEFAULT_PATH, as_of: str | None = None) -> dict:
    """Importe un relevé. Le même relevé importé deux fois ne double rien."""
    raw = Path(csv_path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()[:16]
    data = load(path)
    already = digest in data["statements"]
    reader = csv.DictReader(raw.decode("utf-8-sig").splitlines(), delimiter=_delimiter(raw))
    rows = []
    for n, row in enumerate(reader, start=2):
        row = {COLUMNS.get((k or "").strip().lower()): (v or "").strip() for k, v in row.items()}
        if not row.get("exchange") or not row.get("asset") or not row.get("quantity"):
            raise ValueError(f"Ligne {n} incomplète : plateforme, actif et quantité sont obligatoires.")
        rows.append(row)
    default_date = as_of or datetime.fromtimestamp(Path(csv_path).stat().st_mtime, timezone.utc).date().isoformat()
    for row in rows:
        set_line(data, row["exchange"], row["asset"], row["quantity"].replace(",", "."),
                 row.get("as_of") or default_date, f"relevé {digest}")
    data["statements"].setdefault(digest, {"file": Path(csv_path).name,
                                           "imported_at": datetime.now(timezone.utc).isoformat()})
    save(data, path)
    return {"statement": digest, "already_imported": already, "lines": len(rows)}


def _delimiter(raw: bytes) -> str:
    head = raw.decode("utf-8-sig").splitlines()[0] if raw else ""
    return ";" if head.count(";") > head.count(",") else ","


def valuate(data: dict, prices: dict, now_ms: int, max_age_s: float = MAX_PRICE_AGE_S) -> dict:
    """`prices` : {"plateforme:ACTIF": (prix_eur, horodatage_ms)} ou absent.
    Une ligne sans prix ou au prix périmé n'est pas valorisée, et le total est alors
    marqué partiel : il n'est jamais présenté comme la valeur totale."""
    lines, total, missing = [], 0.0, 0
    for key, line in sorted(data["lines"].items()):
        if line["asset"] == "EUR":
            price, ts = 1.0, now_ms
        else:
            price, ts = prices.get(key) or (None, None)
        fresh = (price is not None and ts is not None and price > 0
                 and -60 <= (now_ms - ts) / 1000 <= max_age_s)   # 1 min d'écart d'horloge toléré
        value = line["quantity"] * price if fresh else None
        if fresh:
            total += value
        else:
            missing += 1
        lines.append({**line, "price_eur": price if fresh else None, "price_ms": ts,
                      "value_eur": value, "status": "ok" if fresh else UNAVAILABLE})
    return {"lines": lines, "total_eur": total, "complete": missing == 0, "missing": missing}


async def fetch_prices(data: dict, now_ms: int | None = None) -> tuple[dict, int]:
    """Cours publics en euros, par plateforme (aucune clé). Une erreur = pas de prix."""
    from trading_agent.exchanges import PublicExchange
    prices, by_exchange = {}, {}
    for key, line in data["lines"].items():
        if line["asset"] != "EUR":
            by_exchange.setdefault(line["exchange"], []).append((key, f"{line['asset']}/EUR"))
    for exchange, wanted in by_exchange.items():
        try:
            async with PublicExchange(exchange) as ex:
                for key, symbol in wanted:
                    try:
                        asked_ms = ex.now_ms()
                        t = await ex.ticker(symbol)
                        # Kraken ne date pas ses tickers : on prend l'heure de la demande.
                        if t.get("last"):
                            prices[key] = (float(t["last"]), int(t.get("timestamp") or asked_ms))
                    except Exception:
                        pass
        except Exception:
            pass
    # Heure lue APRÈS toutes les requêtes : aucun prix ne paraît venir du futur.
    return prices, now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)


def _e(x: float) -> str:
    return f"{x:,.2f} €".replace(",", " ").replace(".", ",")


def _when(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d/%m/%Y %H:%M") if ms else "—"


def render(v: dict) -> str:
    out = ["Portefeuille réel (lecture seule, aucun compte connecté)", ""]
    for ln in v["lines"]:
        value = _e(ln["value_eur"]) if ln["value_eur"] is not None else UNAVAILABLE
        price = (f"prix {_e(ln['price_eur'])} au {_when(ln['price_ms'])}" if ln["price_eur"]
                 else "prix : —")
        out.append(f"  {ln['exchange']:<8} {ln['asset']:<6} {ln['quantity']:.8g} (quantités au "
                   f"{ln['as_of']}) — {price} — {value}")
    if not v["lines"]:
        out.append("  Aucune ligne. Ajoutez-en avec `holdings set` ou `holdings import`.")
    elif v["complete"]:
        out += ["", f"Total : {_e(v['total_eur'])}"]
    else:
        out += ["", f"Total partiel : {_e(v['total_eur'])} — {v['missing']} ligne(s) non valorisée(s), "
                    "ce n'est PAS la valeur totale."]
    return "\n".join(out)


def paper_summary(state_path: str | Path) -> dict | None:
    """Essai papier, lu sans rien modifier (comparaison avec le vrai portefeuille)."""
    try:
        st = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {"equity": st.get("equity"), "cash": st.get("cash"),
            "positions": {s: p.get("quantity") for s, p in (st.get("positions") or {}).items()}}


PAPER_STATES = {"essai papier principal": "data/paper_state.json",
                "essai papier kraken": "data/kraken/paper_state.json"}


def render_paper(summaries: dict) -> str:
    out = ["", "À comparer : essais papier (argent fictif, rien n'est modifié)"]
    for label, s in summaries.items():
        if not s:
            continue
        pos = ", ".join(f"{q:.8g} {sym}" for sym, q in s["positions"].items()) or "aucune position"
        out.append(f"  {label} : capital {_e(s['equity'] or 0)}, cash {_e(s['cash'] or 0)}, {pos}")
    return "\n".join(out) if len(out) > 2 else ""


def main(args) -> None:
    path = args.file
    if args.action == "import":
        r = import_csv(args.csv, path, args.date)
        note = " (relevé déjà importé : quantités remplacées, rien n'est doublé)" if r["already_imported"] else ""
        print(f"{r['lines']} ligne(s) importée(s){note}.")
    elif args.action == "set":
        data = load(path)
        set_line(data, args.exchange, args.asset, args.quantity,
                 args.date or datetime.now(timezone.utc).date().isoformat(), "saisie manuelle")
        save(data, path)
        print("Ligne enregistrée." if args.quantity else "Ligne retirée (quantité 0).")
    else:
        data = load(path)
        prices, now_ms = asyncio.run(fetch_prices(data))
        print(render(valuate(data, prices, now_ms)))
        print(render_paper({k: paper_summary(v) for k, v in PAPER_STATES.items()}))
