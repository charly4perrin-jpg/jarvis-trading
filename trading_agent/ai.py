"""IA d'avis (Grok via xAI, ou ChatGPT via OpenAI) : du langage clair, jamais des décisions.

Les deux fournisseurs passent par leur API officielle « Responses » (même format).

Ce que Grok fait dans Jarvis :
- interpréter les actualités officielles des plateformes (en plus de l'information
  vérifiée, jamais à sa place) ;
- expliquer simplement un fil de décision, à la demande.

Ce que Grok ne peut pas faire : déclencher un ordre, modifier le risque, la
configuration ou une stratégie. Ses réponses sont marquées « non vérifiées ».

Sécurité :
- la clé vient uniquement du fichier .env (XAI_API_KEY) ; elle n'apparaît jamais dans
  le journal, la base, la page ni les messages d'erreur ;
- `store: false` : xAI ne conserve pas les échanges ;
- budget d'appels par jour, délai maximal par appel ;
- sans clé, ou en cas de panne, Jarvis fonctionne exactement comme avant.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

API_URL = "https://api.x.ai/v1/responses"
PROVIDERS = {
    "xai": {"label": "Grok", "url": API_URL, "env": "XAI_API_KEY", "console": "https://console.x.ai"},
    "openai": {"label": "ChatGPT", "url": "https://api.openai.com/v1/responses", "env": "OPENAI_API_KEY",
               "console": "https://platform.openai.com/api-keys"},
}
SYSTEM = ("Tu es l'assistant d'analyse de Jarvis, un système de trading crypto en argent fictif. "
          "Réponds en français simple, en 2 à 4 phrases, pour un débutant. Tu donnes un éclairage, "
          "jamais de conseil d'achat ou de vente, jamais de prédiction de prix. Si une information "
          "manque, dis-le au lieu de supposer.")


class AIUnavailable(RuntimeError):
    pass


def _post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def output_text(data: dict) -> str:
    parts = []
    for item in data.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    parts.append(c["text"])
    return "\n".join(parts).strip()


class GrokClient:
    """Client d'API « Responses » (xAI par défaut, ou OpenAI avec provider="openai")."""

    def __init__(self, api_key: str | None, model: str, budget_path: str, max_calls_per_day: int = 40,
                 timeout_s: float = 30, transport: Callable = _post, provider: str = "xai"):
        self._key = api_key or None
        self.provider = provider
        self.label = PROVIDERS[provider]["label"]
        self.url = PROVIDERS[provider]["url"]
        self.model = model
        self.budget_path = Path(budget_path)
        self.max_calls = max_calls_per_day
        self.timeout_s = timeout_s
        self.transport = transport
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    def _budget(self) -> dict:
        today = datetime.now(timezone.utc).date().isoformat()
        try:
            data = json.loads(self.budget_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return data if data.get("day") == today else {"day": today, "calls": 0, "tokens": 0}

    def usage(self) -> dict:
        b = self._budget()
        return {"enabled": self.enabled, "provider": self.provider, "label": self.label,
                "model": self.model, "calls_today": b["calls"],
                "max_calls_per_day": self.max_calls, "tokens_today": b["tokens"], "last_error": self.last_error}

    def ask(self, prompt: str, max_output_tokens: int = 350) -> str:
        if not self.enabled:
            raise AIUnavailable(f"{self.label} n'est pas branché : aucune clé {PROVIDERS[self.provider]['env']} "
                                "dans le fichier .env.")
        budget = self._budget()
        if budget["calls"] >= self.max_calls:
            raise AIUnavailable(f"budget du jour atteint ({self.max_calls} appels) : reprise demain.")
        body = {"model": self.model, "store": False, "max_output_tokens": max_output_tokens,
                "input": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}
        if self.provider == "openai":
            # Modèles à raisonnement : pas de température ; raisonnement coupé pour des avis courts.
            body["reasoning"] = {"effort": "none"}
        else:
            body["temperature"] = 0.2
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        budget["calls"] += 1   # compté avant l'appel : un appel qui échoue coûte peut-être quand même
        self._save(budget)
        try:
            data = self.transport(self.url, headers, body, self.timeout_s)
        except urllib.error.HTTPError as exc:
            reason = ""
            try:   # code d'erreur renvoyé par le fournisseur (ex. insufficient_quota) ; jamais la clé
                err = json.loads(exc.read().decode("utf-8")).get("error") or {}
                code = err.get("code") or err.get("type") or ""
                reason = {"insufficient_quota": "plus de crédit sur le compte : ajoutez du crédit dans Billing",
                          "credit_balance_exhausted": "plus de crédit sur le compte : ajoutez du crédit dans Billing",
                          "rate_limit_exceeded": "trop de requêtes, réessai plus tard",
                          "invalid_api_key": "clé refusée : vérifiez-la ou créez-en une nouvelle"}.get(code, code)
            except Exception:
                pass
            self.last_error = f"réponse HTTP {exc.code} de {self.label}" + (f" ({reason})" if reason else "")
            raise AIUnavailable(self.last_error) from None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__} pendant l'appel à {self.label}"
            raise AIUnavailable(self.last_error) from None
        text = output_text(data)
        if not text:
            self.last_error = f"réponse vide de {self.label}"
            raise AIUnavailable(self.last_error)
        budget["tokens"] += int((data.get("usage") or {}).get("total_tokens") or 0)
        self._save(budget)
        self.last_error = None
        return text

    def _save(self, budget: dict) -> None:
        try:
            self.budget_path.parent.mkdir(parents=True, exist_ok=True)
            self.budget_path.write_text(json.dumps(budget), encoding="utf-8")
        except OSError:
            pass


AIClient = GrokClient   # nom générique


def make_client(settings, transport: Callable = _post) -> GrokClient:
    """Choisit le fournisseur : AI_PROVIDER s'il est imposé, sinon celui qui a une clé
    (ChatGPT d'abord, puis Grok)."""
    keys = {"openai": settings.openai_api_key, "xai": settings.xai_api_key}
    provider = settings.ai_provider
    if provider == "auto":
        provider = next((p for p in ("openai", "xai") if keys[p] and keys[p].get_secret_value()), "xai")
    secret = keys[provider]
    model = settings.openai_model if provider == "openai" else settings.xai_model
    return GrokClient(secret.get_secret_value() if secret else None, model, settings.ai_budget_path,
                      settings.ai_max_calls_per_day, transport=transport, provider=provider)


def news_prompt(item: dict) -> str:
    return (f"Avis officiel publié par la plateforme {item['exchange']} : « {item['title']} » "
            f"(type : {item['kind']}, état : {item.get('status')}, impact déclaré : {item.get('impact')}). "
            f"Jarvis surveille ces actifs : {', '.join(item.get('assets') or []) or 'aucun cité'}. "
            "Explique ce que cet avis peut concrètement changer pour quelqu'un qui achète et vend "
            "au comptant sur cette plateforme (dépôts, retraits, ordres, prix). Ne dis rien que l'avis ne permet pas d'affirmer.")


def thread_prompt(events: list[dict]) -> str:
    lines = [f"- {e.get('source')} → {e.get('target')} : {e.get('result')}" for e in events[:40]]
    return ("Voici, dans l'ordre, les messages échangés entre les agents de Jarvis pendant un cycle "
            "(argent fictif). Explique simplement ce qui s'est passé et pourquoi la décision finale "
            "a été prise, sans répéter chaque message :\n" + "\n".join(lines))
