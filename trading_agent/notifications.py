"""Notifications : prévenir de ce qui compte, sans noyer sous les alertes.

Le `Notifier` écoute le bus d'événements et applique quelques règles :
- un ordre exécuté, un arrêt, une reprise ou une alerte de sécurité -> toujours ;
- un refus du risque -> une seule fois par motif et par jour ;
- une erreur de cycle -> à la première erreur, puis quand tout est rétabli
  (pas une alerte par cycle en panne) ;
- un mouvement du laboratoire -> noté dans la page, sans fenêtre à l'écran ;
- un bilan par jour, au premier cycle après 8 h.

Deux niveaux : "important" (affiché aussi par macOS) et "info" (page seulement).
Chaque notification est écrite dans un fichier, lu par l'interface.
"""
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from trading_agent.agents import eur
from trading_agent.events import Event

SUMMARY_HOUR = 8


def send_macos(title: str, body: str) -> None:
    """Notification native macOS. Ne bloque jamais et n'échoue jamais bruyamment."""
    def quote(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{quote(body)}" with title "Jarvis" subtitle "{quote(title)}"'
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class EmailSender:
    """Envoie les alertes importantes par e-mail (SMTP sécurisé). Ne bloque jamais Jarvis :
    l'envoi part dans un fil séparé et une panne est seulement notée. Plafond quotidien pour
    éviter toute avalanche ; le mot de passe n'est jamais écrit ailleurs que dans .env."""

    def __init__(self, to: str, host: str, port: int, user: str, password: str,
                 max_per_day: int = 20, smtp_factory=None, background: bool = True):
        import smtplib
        self.to, self.host, self.port, self.user = to, host, port, user
        self._password = password
        self.max_per_day = max_per_day
        self.smtp_factory = smtp_factory or smtplib.SMTP_SSL
        self.background = background
        self.day, self.sent = None, 0
        self.last_error: str | None = None

    def __call__(self, title: str, body: str) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if today != self.day:
            self.day, self.sent = today, 0
        if self.sent > self.max_per_day:
            return
        self.sent += 1
        if self.sent > self.max_per_day:
            title, body = "plafond d'e-mails atteint", (
                f"Plus de {self.max_per_day} alertes aujourd'hui : les suivantes restent visibles dans "
                "la page Jarvis (Alertes), mais ne sont plus envoyées par e-mail avant demain.")
        if self.background:
            import threading
            threading.Thread(target=self._send, args=(title, body), daemon=True).start()
        else:
            self._send(title, body)

    def _send(self, title: str, body: str) -> None:
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = f"Jarvis — {title}"
        msg["From"] = self.user
        msg["To"] = self.to
        msg.set_content(f"{body}\n\n—\nArgent fictif (paper trading). Message automatique de Jarvis, "
                        "envoyé depuis ton Mac. Pour arrêter : retirer NOTIFY_EMAIL_TO du fichier .env.")
        try:
            with self.smtp_factory(self.host, self.port, timeout=15) as smtp:
                smtp.login(self.user, self._password)
                smtp.send_message(msg)
            self.last_error = None
        except Exception as exc:   # jamais le mot de passe dans l'erreur
            self.last_error = f"envoi impossible : {type(exc).__name__}"


def combine(*senders):
    """Plusieurs canaux (Mac, e-mail) ; un canal en panne n'empêche pas les autres."""
    active = [s for s in senders if s]
    if not active:
        return None

    def send(title: str, body: str) -> None:
        for s in active:
            try:
                s(title, body)
            except Exception:
                pass
    return send


def email_sender(settings):
    if not (settings.notify_email_to and settings.smtp_user and settings.smtp_password
            and settings.smtp_password.get_secret_value()):
        return None
    return EmailSender(settings.notify_email_to, settings.smtp_host, settings.smtp_port, settings.smtp_user,
                       settings.smtp_password.get_secret_value(), settings.notify_email_max_per_day)


class Notifier:
    def __init__(self, path: str, sender: Callable[[str, str], None] | None = None,
                 portfolio: Callable[[], object] | None = None, daily_summary: bool = True,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sender = sender
        self.portfolio = portfolio
        self.daily_summary = daily_summary
        self.now = now
        self.items: list[dict] = []
        if self.path.exists():
            with self.path.open(encoding="utf-8") as f:
                self.items = [json.loads(line) for line in f if line.strip()][-500:]
        self.error_cycles = 0
        self.refusals_seen: set[tuple[str, str]] = set()
        self.fills = self.cycles = 0
        last = next((n for n in reversed(self.items) if n["kind"] == "summary"), None)
        # Au tout premier démarrage, pas de bilan immédiat : on part d'aujourd'hui.
        self.summary_day = last["day"] if last else self.now().date().isoformat()
        self.summary_equity = last["equity"] if last else None

    def notify(self, kind: str, level: str, title: str, body: str,
               correlation_id: str | None = None, **extra) -> dict:
        item = {"id": uuid.uuid4().hex, "ts": datetime.now(timezone.utc).isoformat(),
                "kind": kind, "level": level, "title": title, "body": body,
                "correlation_id": correlation_id, **extra}
        self.items.append(item)
        del self.items[:-500]
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
        if level == "important" and self.sender:
            self.sender(title, body)
        return item

    def on_event(self, event: Event) -> None:
        corr, kind = event.correlation_id, event.type
        if kind == "execution.fill":
            self.fills += 1
            side = "Achat simulé" if event.payload.get("side") == "buy" else "Vente simulée"
            self.notify("trade", "important", side, event.result, corr)
        elif kind == "execution.rejected":
            self.notify("trade", "info", "Ordre simulé rejeté", event.result, corr)
        elif kind == "risk.decision" and event.status == "rejected":
            key = (self.now().date().isoformat(), event.payload.get("reason", event.result))
            if key not in self.refusals_seen:
                self.refusals_seen.add(key)
                self.notify("risk", "important", "Achat refusé par le risque",
                            event.result + " Ce motif ne sera plus signalé aujourd'hui.", corr)
        elif kind == "system.halt":
            self.notify("system", "important", "Système arrêté", event.result, corr)
        elif kind == "system.resume":
            self.notify("system", "important", "Système relancé", event.result, corr)
        elif kind == "performance.degradation":
            self.notify("performance", "important", "Stratégie mise en pause", event.result, corr)
        elif kind == "registry.change":
            self.notify("registry", "important", "Statut d'une stratégie modifié", event.result, corr)
        elif kind == "security.alert":
            self.notify("security", "important", "Alerte de sécurité", event.result, corr)
        elif kind == "adversarial.review" and event.status == "rejected":
            self.notify("risk", "info", "Signal écarté par le contradicteur", event.result, corr)
        elif kind == "strategy.lab":
            self.notify("lab", "info", "Laboratoire", event.result, corr)
        elif kind == "cycle.end":
            self._on_cycle_end(event)

    def _on_cycle_end(self, event: Event) -> None:
        corr = event.correlation_id
        if event.status == "error":
            self.error_cycles += 1
            if self.error_cycles == 1:
                self.notify("error", "important", "Erreur pendant un cycle",
                            event.result + " Aucun ordre n'est parti. Vous serez prévenu au retour à la normale.",
                            corr)
            return
        if event.status != "ok":
            return
        self.cycles += 1
        if self.error_cycles:
            self.notify("error", "important", "Fonctionnement rétabli",
                        f"Le cycle s'est déroulé normalement après {self.error_cycles} cycle(s) en erreur.", corr)
            self.error_cycles = 0
        self._maybe_summary()

    def _maybe_summary(self) -> None:
        now = self.now()
        today = now.date().isoformat()
        if not self.daily_summary or today == self.summary_day or now.hour < SUMMARY_HOUR:
            return
        equity = self.portfolio().equity if self.portfolio else None
        parts = []
        if equity is not None:
            parts.append(f"Capital {eur(equity)}")
            if self.summary_equity is not None:
                change = equity - self.summary_equity
                parts.append(f"{'+' if change >= 0 else ''}{eur(change)} depuis le dernier bilan")
        parts.append(f"{self.fills} ordre(s) exécuté(s)")
        parts.append(f"{self.cycles} cycle(s) sans erreur")
        self.notify("summary", "important", "Bilan du jour", ", ".join(parts) + ".",
                    day=today, equity=equity)
        self.summary_day, self.summary_equity = today, equity
        self.fills = self.cycles = 0
