"""Grok via l'API officielle xAI : avis seulement, clé jamais exposée. Aucun appel réseau ici."""
import json
import urllib.error

import pytest

from trading_agent.agents import AIAnalyst
from trading_agent.ai import API_URL, AIUnavailable, GrokClient, output_text
from trading_agent.config import Settings
from trading_agent.events import Event, EventBus
from trading_agent.server import public_config

KEY = "xai-CLE-SECRETE-123"


def fake_xai(answer="Avis de test.", calls=None):
    def transport(url, headers, body, timeout):
        if calls is not None:
            calls.append({"url": url, "headers": headers, "body": body})
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}],
                "usage": {"total_tokens": 42}}
    return transport


def test_request_follows_official_api_and_disables_storage(tmp_path):
    calls = []
    client = GrokClient(KEY, "grok-4.7", str(tmp_path / "b.json"), transport=fake_xai(calls=calls))
    assert client.ask("Bonjour") == "Avis de test."
    call = calls[0]
    assert call["url"] == API_URL == "https://api.x.ai/v1/responses"
    assert call["body"]["model"] == "grok-4.7" and call["body"]["store"] is False
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    assert client.usage()["calls_today"] == 1 and client.usage()["tokens_today"] == 42


def test_without_key_nothing_is_called(tmp_path):
    client = GrokClient(None, "grok-4.7", str(tmp_path / "b.json"), transport=fake_xai())
    assert not client.enabled
    with pytest.raises(AIUnavailable, match="XAI_API_KEY"):
        client.ask("Bonjour")


def test_daily_budget_and_errors_never_leak_the_key(tmp_path):
    client = GrokClient(KEY, "grok-4.7", str(tmp_path / "b.json"), max_calls_per_day=2, transport=fake_xai())
    client.ask("1")
    client.ask("2")
    with pytest.raises(AIUnavailable, match="budget"):
        client.ask("3")

    def refused(url, headers, body, timeout):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
    other = GrokClient(KEY, "grok-4.7", str(tmp_path / "c.json"), transport=refused)
    with pytest.raises(AIUnavailable) as err:
        other.ask("x")
    assert KEY not in str(err.value) and KEY not in (other.last_error or "")
    assert output_text({"output": []}) == ""


def _analyst(tmp_path, key=KEY, transport=None):
    settings = Settings(_env_file=None, xai_api_key=key, ai_budget_path=str(tmp_path / "b.json"),
                        ai_cache_path=str(tmp_path / "cache.json"))
    bus = EventBus(str(tmp_path / "events.jsonl"))
    analyst = AIAnalyst(bus, settings)
    analyst.client.transport = transport or fake_xai()
    return settings, bus, analyst


NEWS = {"kraken": {"items": [
    {"exchange": "kraken", "kind": "incident", "title": "Bitcoin (BTC) Funding Delays", "status": "identified",
     "impact": "minor", "assets": ["BTC"], "relevance": "actif suivi", "verified": True, "interpretation": None},
    {"exchange": "kraken", "kind": "incident", "title": "KAVA deposits paused", "status": "monitoring",
     "impact": "minor", "assets": [], "relevance": "autre", "verified": True, "interpretation": None}]}}


@pytest.mark.asyncio
async def test_news_get_an_unverified_opinion_next_to_the_verified_fact(tmp_path):
    calls = []
    settings, bus, analyst = _analyst(tmp_path, transport=fake_xai("Les dépôts BTC peuvent tarder.", calls))
    news = json.loads(json.dumps(NEWS))
    assert await analyst.interpret_news(news, "n1") == 1          # seul l'avis pertinent est interprété
    btc, kava = news["kraken"]["items"]
    assert btc["verified"] is True and btc["interpretation"]["verified"] is False
    assert kava["interpretation"] is None
    again = json.loads(json.dumps(NEWS))
    assert await analyst.interpret_news(again, "n2") == 0 and len(calls) == 1   # mis en cache
    assert again["kraken"]["items"][0]["interpretation"]["text"] == "Les dépôts BTC peuvent tarder."
    journal = (tmp_path / "events.jsonl").read_text()
    assert "ai.interpretation" in journal and KEY not in journal
    assert not any(KEY in json.dumps(v, default=str) for v in public_config(settings).values())
    assert "xai_api_key" not in public_config(settings)


@pytest.mark.asyncio
async def test_explanation_is_cached_and_grok_cannot_emit_decisions(tmp_path):
    calls = []
    _, bus, analyst = _analyst(tmp_path, transport=fake_xai("Rien n'a été acheté.", calls))
    events = [{"source": "strategy_analyst", "target": "coordinator", "result": "Aucun signal."}]
    first = await analyst.explain("fil1", events)
    second = await analyst.explain("fil1", events)
    assert first["text"] == second["text"] and second["cached"] and len(calls) == 1
    for forbidden in ("trade.proposal", "risk.decision", "execution.fill", "registry.change", "system.resume"):
        with pytest.raises(PermissionError):
            bus.publish(Event(source="ai_analyst", target="execution", type=forbidden, correlation_id="x"))


@pytest.mark.asyncio
async def test_without_key_the_agent_stays_silent(tmp_path):
    _, bus, analyst = _analyst(tmp_path, key=None)
    news = json.loads(json.dumps(NEWS))
    assert await analyst.interpret_news(news, "n") == 0 and not bus.events
    with pytest.raises(AIUnavailable):
        await analyst.explain("fil", [{"source": "a", "target": "b", "result": "x"}])


def test_server_reports_grok_off_and_refuses_explanations(tmp_path):
    from tests.test_server import make_client, wait_for
    with make_client(tmp_path) as client:
        assert client.get("/api/ai").json()["enabled"] is False
        client.post("/api/cycle")
        events = wait_for(client, "cycle.end")
        thread = next(e["correlation_id"] for e in events if e["type"] == "cycle.end")
        reply = client.post("/api/explain", json={"thread": thread})
        assert reply.status_code == 503 and "XAI_API_KEY" in reply.json()["detail"]


OPENAI_KEY = "sk-test-OPENAI-SECRET-456"


def test_provider_is_chosen_by_available_key(tmp_path):
    from trading_agent.ai import make_client
    base = {"_env_file": None, "ai_budget_path": str(tmp_path / "b.json")}
    assert make_client(Settings(**base)).provider == "xai" and not make_client(Settings(**base)).enabled
    assert make_client(Settings(**base, xai_api_key=KEY)).label == "Grok"
    both = make_client(Settings(**base, xai_api_key=KEY, openai_api_key=OPENAI_KEY))
    assert both.provider == "openai" and both.label == "ChatGPT" and both.model == "gpt-6.1-terra"
    forced = make_client(Settings(**base, xai_api_key=KEY, openai_api_key=OPENAI_KEY, ai_provider="xai"))
    assert forced.provider == "xai"


def test_chatgpt_request_uses_official_openai_responses_api(tmp_path):
    from trading_agent.ai import make_client
    calls = []
    client = make_client(Settings(_env_file=None, openai_api_key=OPENAI_KEY, ai_budget_path=str(tmp_path / "b.json")),
                         transport=fake_xai("Rien à signaler.", calls))
    assert client.ask("Bonjour") == "Rien à signaler."
    call = calls[0]
    assert call["url"] == "https://api.openai.com/v1/responses"
    assert call["body"]["store"] is False and "temperature" not in call["body"]
    assert call["body"]["reasoning"] == {"effort": "none"} and call["body"]["model"] == "gpt-6.1-terra"
    assert call["headers"]["Authorization"] == f"Bearer {OPENAI_KEY}"


@pytest.mark.asyncio
async def test_chatgpt_opinions_are_labelled_and_key_stays_hidden(tmp_path):
    settings = Settings(_env_file=None, openai_api_key=OPENAI_KEY, ai_budget_path=str(tmp_path / "b.json"),
                        ai_cache_path=str(tmp_path / "cache.json"))
    bus = EventBus(str(tmp_path / "events.jsonl"))
    analyst = AIAnalyst(bus, settings)
    analyst.client.transport = fake_xai("Les retraits BTC peuvent tarder.")
    news = json.loads(json.dumps(NEWS))
    await analyst.interpret_news(news, "n")
    event = next(e for e in bus.events if e.type == "ai.interpretation")
    assert event.result.startswith("Avis de ChatGPT (non vérifié)")
    journal = (tmp_path / "events.jsonl").read_text()
    assert OPENAI_KEY not in journal and "openai_api_key" not in public_config(settings)
