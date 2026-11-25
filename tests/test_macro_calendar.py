"""Calendrier économique : lecture seule, prudence quand le fichier est douteux,
aucun effet sur les décisions."""
import json
from datetime import datetime, timezone

import pytest

from tests.test_agents import BUY, FakeExchange
from trading_agent import macro_calendar
from trading_agent.agents import Coordinator
from trading_agent.config import Settings
from trading_agent.events import EventBus
from trading_agent.paper import PaperBroker

NOW = datetime(2026, 12, 7, 12, 0, tzinfo=timezone.utc)
FED = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"


def write(tmp_path, events, covered="2027-03-01"):
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"mis_a_jour_le": "2026-11-18", "couvert_jusqu_au": covered,
                                "evenements": events}), encoding="utf-8")
    return path


def fomc(day="2026-12-09", **extra):
    return {"date": day, "heure": "14:00", "fuseau": "America/New_York", "zone": "États-Unis",
            "nom": "Décision de taux de la Fed (FOMC)", "source": FED} | extra


def test_shipped_file_is_valid_and_uses_official_sources_only():
    cal = macro_calendar.load(now=datetime(2026, 11, 18, tzinfo=timezone.utc))
    assert cal["error"] is None and not cal["stale"] and len(cal["events"]) >= 20
    assert {e["source"].split("/")[2] for e in cal["events"]} <= set(macro_calendar.OFFICIAL_HOSTS)


def test_local_time_is_converted_to_utc(tmp_path):
    cal = macro_calendar.load(write(tmp_path, [fomc()]), NOW)
    assert cal["events"][0]["at"] == "2026-12-09T19:00:00+00:00"     # 14 h à New York en hiver
    assert [e["name"] for e in macro_calendar.upcoming(cal, NOW)] == ["Décision de taux de la Fed (FOMC)"]


def test_past_and_far_events_are_not_upcoming(tmp_path):
    cal = macro_calendar.load(write(tmp_path, [fomc("2026-12-01"), fomc("2027-01-27")]), NOW)
    assert macro_calendar.upcoming(cal, NOW) == []
    assert "Aucune grande annonce" in macro_calendar.summary(cal, NOW)


def test_missing_file_is_unknown_never_empty(tmp_path):
    cal = macro_calendar.load(tmp_path / "absent.json", NOW)
    assert cal["error"] and cal["events"] == []
    assert macro_calendar.summary(cal, NOW).startswith("Calendrier économique inconnu")


@pytest.mark.parametrize("bad", [
    {"date": "2026-13-40"}, {"heure": "25:00"}, {"fuseau": "Mars/Olympus"}, {"nom": ""},
    {"source": "https://exemple-forex.com/calendar"}, {"source": "http://www.federalreserve.gov/x"},
])
def test_one_doubtful_line_rejects_the_whole_file(tmp_path, bad):
    cal = macro_calendar.load(write(tmp_path, [fomc("2026-12-10"), fomc() | bad]), NOW)
    assert cal["error"] and cal["events"] == []


def test_broken_json_is_unknown(tmp_path):
    path = tmp_path / "cal.json"
    path.write_text("{pas du json", encoding="utf-8")
    assert macro_calendar.load(path, NOW)["error"]


def test_event_after_announced_coverage_is_refused(tmp_path):
    assert macro_calendar.load(write(tmp_path, [fomc("2027-05-01")], covered="2027-03-01"), NOW)["error"]


def test_short_coverage_is_flagged_and_never_says_nothing_planned(tmp_path):
    cal = macro_calendar.load(write(tmp_path, [fomc("2026-12-08")], covered="2026-12-10"), NOW)
    assert cal["stale"]
    text = macro_calendar.summary(cal, NOW)
    assert "à mettre à jour" in text and "impossible de dire" in text and "Aucune grande annonce" not in text


def test_agent_publishes_calendar_as_information(tmp_path):
    bus = EventBus(str(tmp_path / "events.jsonl"))
    coordinator = Coordinator(bus, Settings(_env_file=None), PaperBroker(1000.0, 0.0025, 10, None, None))
    coordinator.news.check_calendar("cal", write(tmp_path, [fomc()]), NOW)
    event = next(e for e in bus.events if e.type == "news.calendar")
    assert event.source == "news_analyst" and event.status == "ok"
    assert "FOMC" in event.result and "aucun effet sur les décisions" in event.result
    coordinator.news.check_calendar("cal2", tmp_path / "absent.json", NOW)
    assert bus.events[-1].status == "error" and "inconnu" in bus.events[-1].result


@pytest.mark.asyncio
async def test_calendar_has_no_effect_on_trading_decisions(tmp_path):
    outcomes = []
    for with_calendar in (False, True):
        bus = EventBus(str(tmp_path / f"e{with_calendar}.jsonl"))
        broker = PaperBroker(1000.0, 0.0025, 10, None, None)
        coordinator = Coordinator(bus, Settings(_env_file=None, regime_filter=False), broker)
        if with_calendar:   # une décision de la Fed dans 2 heures
            soon = datetime.now(timezone.utc)
            event = fomc(soon.date().isoformat(), heure="23:59", fuseau="UTC")
            coordinator.news.check_calendar("cal", write(tmp_path, [event], covered="2099-01-01"))
            assert coordinator.news.calendar["events"]
        corr = await coordinator.run_cycle(FakeExchange(BUY), "fake", "BTC/EUR", "1m")
        decisions = [(e.type, e.status) for e in bus.thread(corr)]
        outcomes.append((decisions, len(broker.portfolio.positions), round(broker.portfolio.cash, 6)))
    assert outcomes[0] == outcomes[1]
