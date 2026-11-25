"""Événements échangés entre agents, bus de communication et journal chaîné.

Chaque message entre deux agents est un `Event`. Le bus :
- vérifie que l'agent émetteur a le droit d'émettre ce type d'événement ;
- écrit l'événement dans un journal où chaque ligne contient l'empreinte de la
  précédente (toute modification ultérieure casse la chaîne et se détecte) ;
- prévient les abonnés (moniteur de sécurité, affichage, future interface).
"""
import hashlib
import json
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = 2   # v1 : journal d'origine, sans version, priorité ni réponse

# Qui a le droit d'émettre quoi (préfixe du type d'événement -> agents autorisés).
PERMISSIONS: dict[str, set[str]] = {
    "cycle.": {"coordinator"},
    "agent.": {"coordinator"},
    "system.halt": {"coordinator", "security_monitor"},
    "system.resume": {"coordinator"},
    "registry.": {"coordinator"},
    "config.": {"coordinator"},        # version de la configuration à chaque démarrage  # promotions : action humaine uniquement
    "market.": {"market_intelligence"},
    "regime.": {"regime_detector"},
    "exchange.": {"exchange_monitor"},
    "allocation.": {"portfolio_optimizer"},
    "performance.": {"performance_analyst"},
    "routing.": {"execution_intelligence"},
    "arbitrage.": {"arbitrage_analyst"},
    "learning.": {"learning_manager"},
    "news.": {"news_analyst"},
    "ai.": {"ai_analyst"},           # avis de Grok : jamais de décision
    "adversarial.": {"adversarial_analyst"},
    # Tout agent peut signaler une anomalie : c'est le Security Monitor qui en juge.
    "anomaly.": {"coordinator", "market_intelligence", "regime_detector", "strategy_analyst",
                 "adversarial_analyst", "trade_proposer", "risk_manager", "execution",
                 "portfolio_manager", "auditor", "exchange_monitor", "portfolio_optimizer",
                 "performance_analyst", "execution_intelligence", "arbitrage_analyst",
                 "learning_manager", "news_analyst"},
    "strategy.": {"strategy_analyst"},
    "trade.": {"trade_proposer"},
    "risk.": {"risk_manager"},
    "execution.": {"execution"},
    "portfolio.": {"portfolio_manager"},
    "security.": {"security_monitor"},
    "audit.": {"auditor"},
}


class Event(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str
    target: str
    type: str
    correlation_id: str          # identifiant du « fil » : relie toute une décision
    status: Literal["ok", "rejected", "error"] = "ok"
    priority: Literal["low", "normal", "high", "critical"] = "normal"
    reply_to: str | None = None  # identifiant de l'événement auquel celui-ci répond
    schema_version: int = SCHEMA_VERSION
    result: str = ""             # phrase lisible décrivant ce qui s'est passé
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = None
    prev_hash: str = ""
    hash: str = ""

    def compute_hash(self) -> str:
        exclude = {"hash"}
        if self.schema_version == 1:  # empreinte calculée comme à l'origine
            exclude |= {"priority", "reply_to", "schema_version"}
        body = self.model_dump(mode="json", exclude=exclude)
        raw = json.dumps(body, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_line(line: str) -> Event:
    """Lit une ligne du journal. Une ligne sans version vient du journal d'origine (v1)."""
    raw = json.loads(line)
    raw.setdefault("schema_version", 1)
    return Event.model_validate(raw)


class _Contract(BaseModel):
    model_config = ConfigDict(extra="allow")   # champs obligatoires stricts, extras permis


class RegimePayload(_Contract):
    symbol: str
    regime: dict | None
    allowed: dict[str, bool]


class AdversarialPayload(_Contract):
    symbol: str
    strategy: str
    verdict: Literal["pass", "reject"]
    objections: list[dict]


class ProposalPayload(_Contract):
    proposal_id: str
    symbol: str
    side: Literal["buy", "sell"]
    entry_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    created_at: str
    valid_until: str
    strategy: str


class RiskPayload(_Contract):
    approved: bool
    proposal_id: str | None = None


class ExchangeStatusPayload(_Contract):
    exchange: str
    status: Literal["ok", "degraded", "down", "unknown"]
    tradable: bool
    problems: list[str]


class AllocationPayload(_Contract):
    symbol: str
    correlations: list[dict]
    correlated_open_risk: float = Field(ge=0)
    max_correlated_risk: float = Field(ge=0)


class RoutingPayload(_Contract):
    symbol: str
    choice: Literal["market", "limit"]
    options: dict


class ArbitragePayload(_Contract):
    pairs: list[dict]


class ScanPayload(_Contract):
    exchange: str
    watchlist: list[str]
    candidates: list[dict]


# Contrats de données : le contenu de ces événements est validé avant publication.
CONTRACTS: dict[str, type[BaseModel]] = {
    "regime.detected": RegimePayload, "adversarial.review": AdversarialPayload,
    "trade.proposal": ProposalPayload, "risk.decision": RiskPayload, "market.scan": ScanPayload,
    "routing.plan": RoutingPayload, "arbitrage.scan": ArbitragePayload,
    "exchange.status": ExchangeStatusPayload, "allocation.assessment": AllocationPayload,
}


class EventBus:
    def __init__(self, path: str | None = None, halt_path: str | None = None,
                 db_path: str | None = None):
        self.path = Path(path) if path else None
        self.halt_path = Path(halt_path) if halt_path else None
        self.subscribers: list[Callable[[Event], None]] = []
        self.events: deque[Event] = deque(maxlen=5000)
        self._halted = False
        self._halt_reason = ""
        self._size = -1
        self._approved: set[str] = set()   # propositions validées par le risque
        self._refused: set[str] = set()    # refus définitifs
        self._executed: set[str] = set()   # propositions déjà exécutées (anti-doublon)
        self._last_hash = ""
        if self.halt_path and self.halt_path.exists():
            # L'arrêt d'urgence survit au redémarrage : seul `resume` le lève.
            self._halted = True
            self._halt_reason = self.halt_path.read_text(encoding="utf-8").strip()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self._last_hash = self._read_last_hash()
                self._size = self.path.stat().st_size
        self.store = None
        if db_path:
            from trading_agent.store import EventStore
            self.store = EventStore(db_path)
            if self.path:
                self.store.sync_from_journal(self.path)   # import / rattrapage au démarrage

    # Quand un fichier d'arrêt est configuré, c'est lui qui fait foi : un arrêt ou une
    # reprise lancés depuis un autre programme (ligne de commande) sont vus tout de suite.
    @property
    def halted(self) -> bool:
        return self.halt_path.exists() if self.halt_path else self._halted

    @property
    def halt_reason(self) -> str:
        if self.halt_path and self.halt_path.exists():
            return self.halt_path.read_text(encoding="utf-8").strip()
        return "" if self.halt_path else self._halt_reason

    def _read_last_hash(self) -> str:
        last = None
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        return json.loads(last)["hash"] if last else ""

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        self.subscribers.append(callback)

    def publish(self, event: Event) -> Event:
        allowed = next((who for prefix, who in PERMISSIONS.items()
                        if event.type.startswith(prefix)), None)
        if allowed is None or event.source not in allowed:
            raise PermissionError(
                f"{event.source} n'a pas le droit d'émettre « {event.type} ».")
        contract = CONTRACTS.get(event.type)
        if contract:
            try:
                contract.model_validate(event.payload)
            except ValidationError as exc:
                raise ValueError(f"Contenu invalide pour « {event.type} » : {exc.errors()[0]['msg']}"
                                 f" ({'.'.join(map(str, exc.errors()[0]['loc']))}).") from exc
        event.payload = json.loads(json.dumps(event.payload, default=str))
        if self.path and self.path.exists() and self.path.stat().st_size != self._size:
            # Un autre programme a écrit dans le journal : on repart de sa dernière ligne
            # pour ne pas casser la chaîne.
            self._last_hash = self._read_last_hash()
        event.prev_hash = self._last_hash
        event.hash = event.compute_hash()
        self._last_hash = event.hash
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(event.model_dump_json() + "\n")
            self._size = self.path.stat().st_size
        if self.store:
            self.store.record(event, self.path)
        self.events.append(event)
        proposal_id = event.payload.get("proposal_id")
        if event.type == "risk.decision" and proposal_id:
            if event.status == "ok" and event.payload.get("approved") is True:
                self._approved.add(proposal_id)
            else:
                self._refused.add(proposal_id)
        if event.type in ("execution.fill", "execution.order_placed") and proposal_id:
            self._executed.add(proposal_id)
        if event.type == "system.halt":
            self._halted, self._halt_reason = True, event.result
            if self.halt_path:
                self.halt_path.parent.mkdir(parents=True, exist_ok=True)
                self.halt_path.write_text(event.result, encoding="utf-8")
        if event.type == "system.resume":
            self._halted, self._halt_reason = False, ""
            if self.halt_path:
                self.halt_path.unlink(missing_ok=True)
        for callback in self.subscribers:
            callback(event)
        return event

    @property
    def recording_ok(self) -> bool:
        """Faux si la base de données est en panne ou en retard sur le journal."""
        return self.store is None or self.store.ok

    def is_approved(self, proposal_id: str | None) -> bool:
        """Vrai seulement si le Risk Manager a validé CETTE proposition, qu'elle n'a
        jamais été refusée et qu'elle n'a pas déjà été exécutée."""
        return (bool(proposal_id) and proposal_id in self._approved
                and proposal_id not in self._refused and proposal_id not in self._executed)

    def thread(self, correlation_id: str) -> list[Event]:
        return [e for e in self.events if e.correlation_id == correlation_id]


def read_events(path: str) -> list[Event]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as f:
        return [parse_line(line) for line in f if line.strip()]


def verify_chain(path: str) -> tuple[bool, int, int | None]:
    """Vérifie le journal. Retourne (intact, nombre d'événements, ligne fautive)."""
    prev = ""
    count = 0
    p = Path(path)
    if not p.exists():
        return True, 0, None
    with p.open(encoding="utf-8") as f:
        for number, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                event = parse_line(line)
            except ValueError:
                return False, count, number
            if event.prev_hash != prev or event.compute_hash() != event.hash:
                return False, count, number
            prev = event.hash
            count += 1
    return True, count, None
