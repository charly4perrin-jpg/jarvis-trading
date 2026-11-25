from datetime import datetime
from types import SimpleNamespace

from trading_agent.events import Event
from trading_agent.notifications import Notifier


def event(type_, status="ok", result="texte", **payload):
    return Event(source="x", target="y", type=type_, correlation_id="fil", status=status,
                 result=result, payload=payload)


def make(tmp_path, when=datetime(2026, 11, 17, 7, 0)):
    clock = {"now": when}
    sent = []
    notifier = Notifier(str(tmp_path / "n.jsonl"), sender=lambda t, b: sent.append((t, b)),
                        portfolio=lambda: SimpleNamespace(equity=3050.0),
                        now=lambda: clock["now"].astimezone())
    return notifier, sent, clock


def test_trades_and_halts_always_notify(tmp_path):
    n, sent, _ = make(tmp_path)
    n.on_event(event("execution.fill", side="buy"))
    n.on_event(event("execution.fill", side="sell"))
    n.on_event(event("system.halt"))
    n.on_event(event("system.resume"))
    assert [t for t, _ in sent] == ["Achat simulé", "Vente simulée", "Système arrêté", "Système relancé"]
    assert n.items[0]["correlation_id"] == "fil"


def test_quiet_events_stay_silent(tmp_path):
    n, sent, _ = make(tmp_path)
    for type_ in ("cycle.start", "market.snapshot", "strategy.no_signal", "portfolio.update"):
        n.on_event(event(type_))
    n.on_event(event("cycle.end"))
    assert sent == [] and n.items == []


def test_same_refusal_is_reported_once_per_day(tmp_path):
    n, sent, clock = make(tmp_path)
    for _ in range(3):
        n.on_event(event("risk.decision", status="rejected", reason="Daily loss limit reached."))
    n.on_event(event("risk.decision", status="rejected", reason="Spread unknown or too wide."))
    assert len(sent) == 2
    clock["now"] = datetime(2026, 11, 18, 7, 0)
    n.on_event(event("risk.decision", status="rejected", reason="Daily loss limit reached."))
    assert len(sent) == 3


def test_errors_notify_once_then_on_recovery(tmp_path):
    n, sent, _ = make(tmp_path)
    for _ in range(4):
        n.on_event(event("cycle.end", status="error"))
    assert [t for t, _ in sent] == ["Erreur pendant un cycle"]
    n.on_event(event("cycle.end"))
    assert sent[-1][0] == "Fonctionnement rétabli" and "4 cycle(s)" in sent[-1][1]
    n.on_event(event("cycle.end"))
    assert len(sent) == 2


def test_lab_moves_are_recorded_without_popup(tmp_path):
    n, sent, _ = make(tmp_path)
    n.on_event(event("strategy.lab"))
    assert sent == [] and n.items[0]["level"] == "info"


def test_daily_summary_once_after_eight_and_survives_restart(tmp_path):
    n, sent, clock = make(tmp_path, datetime(2026, 11, 17, 20, 0))
    n.on_event(event("cycle.end"))
    assert sent == []                                  # jour de l'installation : pas de bilan
    clock["now"] = datetime(2026, 11, 18, 4, 0)
    n.on_event(event("cycle.end"))
    assert sent == []                                  # avant 8 h : on attend
    clock["now"] = datetime(2026, 11, 18, 8, 5)
    n.on_event(event("execution.fill", side="buy"))
    n.on_event(event("cycle.end"))
    assert sent[-1][0] == "Bilan du jour" and "1 ordre(s)" in sent[-1][1] and "3 050,00 €" in sent[-1][1]
    n.on_event(event("cycle.end"))
    assert [t for t, _ in sent].count("Bilan du jour") == 1
    restarted = Notifier(str(tmp_path / "n.jsonl"), now=lambda: clock["now"].astimezone())
    assert restarted.summary_day == "2026-11-18" and restarted.summary_equity == 3050.0


class FakeSMTP:
    sent, logins = [], []

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        FakeSMTP.logins.append((user, password))

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_email_alerts_are_sent_capped_and_never_block():
    from trading_agent.notifications import EmailSender, combine
    FakeSMTP.sent.clear()
    mail = EmailSender("moi@example.com", "smtp.example.com", 465, "moi@example.com", "code-app",
                       max_per_day=2, smtp_factory=FakeSMTP, background=False)
    for i in range(5):
        mail("Achat simulé", f"message {i}")
    subjects = [m["Subject"] for m in FakeSMTP.sent]
    assert subjects == ["Jarvis — Achat simulé", "Jarvis — Achat simulé", "Jarvis — plafond d'e-mails atteint"]
    assert "Argent fictif" in FakeSMTP.sent[0].get_content()

    class Down(FakeSMTP):
        def login(self, user, password):
            raise OSError("serveur injoignable")
    broken = EmailSender("a@b.c", "h", 465, "a@b.c", "code-secret", smtp_factory=Down, background=False)
    broken("x", "y")                                                  # ne lève rien
    assert "code-secret" not in broken.last_error
    calls = []
    combine(lambda t, b: calls.append(t), broken, None)("Arrêt", "z")   # un canal en panne n'empêche pas l'autre
    assert calls == ["Arrêt"]


def test_personal_details_never_reach_config_or_journal():
    from trading_agent.config import Settings
    from trading_agent.server import public_config
    s = Settings(_env_file=None, notify_email_to="moi@example.com", smtp_user="moi@example.com",
                 smtp_password="code-app")
    shown = str(public_config(s))
    assert "moi@example.com" not in shown and "code-app" not in shown
