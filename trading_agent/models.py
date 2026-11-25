from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

@dataclass(frozen=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    confidence: float
    reference_price: float
    stop_price: float
    rationale: str
    timestamp: datetime
    strategy: str = "momentum_demo"
    memo: dict | None = None     # niveaux notés au signal (ex. plus haut de la compression)

@dataclass(frozen=True)
class PaperOrder:
    symbol: str
    side: Side
    quantity: float
    reference_price: float
    stop_price: float
    strategy: str = "momentum_demo"

@dataclass
class PaperPortfolio:
    cash: float
    positions: dict[str, dict]
    equity: float
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    day_key: str | None = None
    week_key: str | None = None
    day_start_equity: float | None = None
    week_start_equity: float | None = None
    consecutive_losses: int = 0
    # Dernière bougie déjà analysée par marché : une seule décision d'achat par bougie.
    analysed: dict[str, int] = field(default_factory=dict)
    # Ordres limite en attente (identifiant -> détail). Leur montant est réservé :
    # il reste dans le cash mais ne peut pas servir à un autre achat.
    pending: dict[str, dict] = field(default_factory=dict)
    # Capital relevé au fil du temps (horodatage ISO, capital), sur 48 h au plus :
    # sert à mesurer la vitesse des pertes sur une fenêtre glissante.
    equity_marks: list = field(default_factory=list)

def utc_now():
    return datetime.now(timezone.utc)
