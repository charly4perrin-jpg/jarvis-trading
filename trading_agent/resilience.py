"""Appels aux plateformes : délai maximal, nouvelles tentatives et coupe-circuit.

- Délai maximal : un appel qui ne répond pas est abandonné (fini les 17 minutes
  d'attente observées le 2026-11-17).
- Nouvelles tentatives : seulement pour les erreurs réseau, avec une attente qui
  double à chaque essai (1 s, 2 s...).
- Coupe-circuit : après plusieurs échecs réseau d'affilée, on cesse d'appeler la
  plateforme pendant un temps de repos ; les appels échouent alors immédiatement.
  Au bout du repos, un seul essai est permis : s'il réussit, le circuit se referme.

Les mesures (latence, erreurs, limites de fréquence, maintenance) alimentent
l'Exchange Monitor.
"""
import asyncio
import json
import time
from pathlib import Path
from collections import deque
from typing import Awaitable, Callable

import ccxt.async_support as ccxt


class CircuitOpen(RuntimeError):
    pass


class ExchangeHealth:
    def __init__(self, exchange_id: str, timeout_s: float = 20, retries: int = 2,
                 base_delay_s: float = 1.0, failure_threshold: int = 3, cooldown_s: float = 300,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self.exchange_id = exchange_id
        self.timeout_s, self.retries, self.base_delay_s = timeout_s, retries, base_delay_s
        self.failure_threshold, self.cooldown_s = failure_threshold, cooldown_s
        self.clock, self.sleep = clock, sleep
        self.calls = self.errors = self.rate_limited = self.consecutive_failures = 0
        self.outcomes: deque[bool] = deque(maxlen=20)       # derniers appels : réussis ?
        self.latencies_ms: deque[float] = deque(maxlen=50)
        self.last_error: str | None = None
        self.last_success: float | None = None
        self.maintenance = False
        self.open_until = 0.0
        self.state_path: Path | None = None

    def persist_to(self, path: str) -> None:
        """Conserve l'état du coupe-circuit sur disque : un redémarrage ne peut pas
        lever une suspension en cours (idée reprise de PumpGrok, réécrite pour Jarvis)."""
        self.state_path = Path(path)
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.consecutive_failures = int(data.get("consecutive_failures", 0))
        self.last_error = data.get("last_error")
        remaining = float(data.get("open_until_epoch", 0)) - time.time()
        if remaining > 0:
            self.open_until = self.clock() + remaining

    def _persist(self) -> None:
        if not self.state_path:
            return
        remaining = max(0.0, self.open_until - self.clock())
        data = {"consecutive_failures": self.consecutive_failures, "last_error": self.last_error,
                "open_until_epoch": time.time() + remaining if remaining else 0}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass   # l'écriture de l'état ne doit jamais empêcher la mesure

    @property
    def state(self) -> str:
        if self.clock() < self.open_until:
            return "open"
        return "half_open" if self.consecutive_failures >= self.failure_threshold else "closed"

    def _failure(self, name: str, exc: BaseException) -> None:
        self.calls += 1
        self.errors += 1
        self.outcomes.append(False)
        self.consecutive_failures += 1
        self.last_error = f"{name} : {type(exc).__name__} {str(exc)[:160]}".strip()
        if isinstance(exc, ccxt.RateLimitExceeded):
            self.rate_limited += 1
        self.maintenance = isinstance(exc, ccxt.OnMaintenance)
        if self.consecutive_failures >= self.failure_threshold:
            self.open_until = self.clock() + self.cooldown_s
        self._persist()

    def _success(self, started: float) -> None:
        self.calls += 1
        self.outcomes.append(True)
        self.latencies_ms.append((self.clock() - started) * 1000)
        was_failing = bool(self.consecutive_failures or self.open_until)
        self.maintenance = False
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.last_success = time.time()
        if was_failing:
            self._persist()

    async def call(self, name: str, factory: Callable[[], Awaitable]):
        if self.state == "open":
            raise CircuitOpen(f"{self.exchange_id} suspendue après {self.consecutive_failures} échecs "
                              f"réseau ; nouvel essai dans {self.open_until - self.clock():.0f} s")
        for attempt in range(self.retries + 1):
            started = self.clock()
            try:
                result = await asyncio.wait_for(factory(), self.timeout_s)
            except (asyncio.TimeoutError, ccxt.NetworkError) as exc:
                self._failure(name, exc)
                if self.state == "open" or attempt == self.retries:
                    raise
                await self.sleep(self.base_delay_s * 2 ** attempt)
                continue
            except ccxt.ExchangeError as exc:
                # Erreur de la plateforme (paire inconnue...) : inutile de réessayer,
                # et ce n'est pas une panne réseau.
                self.calls += 1
                self.errors += 1
                self.last_error = f"{name} : {type(exc).__name__} {str(exc)[:160]}"
                raise
            self._success(started)
            return result

    def snapshot(self) -> dict:
        recent = list(self.outcomes)
        lat = list(self.latencies_ms)
        return {"exchange": self.exchange_id, "circuit": self.state, "calls": self.calls,
                "errors": self.errors, "rate_limited": self.rate_limited,
                "recent_error_rate": (recent.count(False) / len(recent)) if recent else None,
                "latency_ms": sum(lat) / len(lat) if lat else None,
                "latency_max_ms": max(lat) if lat else None,
                "last_error": self.last_error, "last_success": self.last_success,
                "maintenance": self.maintenance}
