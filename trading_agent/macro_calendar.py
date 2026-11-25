"""Calendrier économique en lecture seule : grandes annonces macro (Fed, BCE, inflation
et emploi américains).

Source : un fichier de dates recopiées à la main depuis les pages officielles
(`macro_calendar.json`, avec le lien de chaque source). Aucune requête réseau.

Prudence : un fichier absent, abîmé, avec une seule ligne douteuse ou qui ne couvre
pas les 30 prochains jours n'est jamais présenté comme « aucun événement » : l'état
dit alors clairement que le calendrier est inconnu ou à mettre à jour.

Information seulement : rien ici n'est lu par le moteur de risque ni par les
décisions de trading.
"""
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

DEFAULT_PATH = Path(__file__).with_name("macro_calendar.json")
OFFICIAL_HOSTS = ("www.federalreserve.gov", "www.ecb.europa.eu", "www.bls.gov")
MIN_COVERAGE_DAYS = 30


def _event(raw: dict) -> dict:
    name = str(raw.get("nom") or "").strip()
    if not name:
        raise ValueError("événement sans nom")
    source = str(raw.get("source") or "")
    url = urlparse(source)
    if url.scheme != "https" or url.hostname not in OFFICIAL_HOSTS:
        raise ValueError(f"« {name} » : source non officielle ou absente")
    day = date.fromisoformat(str(raw.get("date")))
    hour = time.fromisoformat(str(raw.get("heure")))
    at = datetime.combine(day, hour, ZoneInfo(str(raw.get("fuseau"))))
    return {"name": name, "zone": str(raw.get("zone") or ""), "at": at.astimezone(timezone.utc).isoformat(),
            "local": f"{day.isoformat()} {hour.strftime('%H:%M')} ({raw.get('fuseau')})", "source": source}


def load(path: str | Path | None = None, now: datetime | None = None) -> dict:
    """Lit et vérifie le fichier. Ne lève jamais : l'erreur est dans le résultat."""
    now = now or datetime.now(timezone.utc)
    path = Path(path) if path else DEFAULT_PATH
    result = {"events": [], "error": None, "stale": True, "covered_until": None, "updated": None,
              "path": str(path)}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        events = sorted((_event(e) for e in doc["evenements"]), key=lambda e: e["at"])
        covered = date.fromisoformat(str(doc["couvert_jusqu_au"]))
        if events and events[-1]["at"][:10] > covered.isoformat():
            raise ValueError("un événement dépasse la date de couverture annoncée")
    except FileNotFoundError:
        result["error"] = "fichier du calendrier absent"
        return result
    except Exception as exc:   # fichier abîmé : on ne montre rien plutôt qu'une liste douteuse
        result["error"] = f"calendrier illisible : {exc}"
        return result
    result.update(events=events, covered_until=covered.isoformat(), updated=doc.get("mis_a_jour_le"),
                  stale=covered < (now + timedelta(days=MIN_COVERAGE_DAYS)).date())
    return result


def upcoming(calendar: dict, now: datetime | None = None, days: int = 7) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    end = now + timedelta(days=days)
    return [e for e in calendar["events"] if now <= datetime.fromisoformat(e["at"]) <= end]


def summary(calendar: dict, now: datetime | None = None, days: int = 7) -> str:
    if calendar["error"]:
        return f"Calendrier économique inconnu ({calendar['error']})."
    soon = upcoming(calendar, now, days)
    head = (f"Calendrier économique à mettre à jour (couvert jusqu'au {calendar['covered_until']} seulement). "
            if calendar["stale"] else "")
    blind = calendar["covered_until"] < ((now or datetime.now(timezone.utc)) + timedelta(days=days)).date().isoformat()
    if blind:
        head += "Au-delà de cette date, impossible de dire ce qui est prévu. "
    if not soon:
        return head if blind else head + f"Aucune grande annonce macro prévue dans les {days} prochains jours."
    listed = " ; ".join(f"{e['name']} le {e['local']}" for e in soon)
    return head + f"Annonces macro des {days} prochains jours : {listed}. Information seulement, aucun effet sur les décisions."
