from __future__ import annotations

from types import SimpleNamespace

import anthropic
import pytest
from pydantic import BaseModel

from app import llm as llm_mod
from app.llm import (
    LLM,
    CostCeilingExceeded,
    LLMOutputError,
    LLMRefusal,
    get_meter,
    parse_web_search_results,
    strict_json_schema,
)


class Thing(BaseModel):
    name: str
    count: int
    tags: list[str] = []
    note: str | None = None


def resp(text="", stop="end_turn", content=None, tokens=(1000, 200), searches=0):
    return SimpleNamespace(
        content=content if content is not None else [SimpleNamespace(type="text", text=text, citations=None)],
        stop_reason=stop,
        stop_details=SimpleNamespace(category="cyber", explanation="x") if stop == "refusal" else None,
        usage=SimpleNamespace(
            input_tokens=tokens[0], output_tokens=tokens[1], cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            server_tool_use=SimpleNamespace(web_search_requests=searches) if searches else None,
        ),
        model="claude-sonnet-5-5",
    )


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_llm(settings, responses, fallback=False, beta_responses=None):
    settings.llm_refusal_fallback = fallback
    client = SimpleNamespace(messages=FakeMessages(responses), beta=SimpleNamespace(messages=FakeMessages(beta_responses or [])))
    return LLM(settings, client=client), client


@pytest.fixture(autouse=True)
def _clear_meters():
    llm_mod._meters.clear()
    yield
    llm_mod._meters.clear()


def test_strict_schema_shape():
    s = strict_json_schema(Thing)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == {"name", "count", "tags", "note"}
    assert "title" not in s and "default" not in str(s)


async def test_structured_ok_and_cost(settings):
    llm, client = make_llm(settings, [resp('{"name":"a","count":2,"tags":[],"note":null}')])
    out = await llm.structured(Thing, purpose="t", system="s", user_content="u", job_id="job_test1")
    assert out == Thing(name="a", count=2)
    call = client.messages.calls[0]
    assert call["model"] == settings.anthropic_model
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == settings.llm_effort
    assert "temperature" not in call and "tool_choice" not in call
    meter = get_meter("job_test1", settings)
    assert meter.calls == 1
    assert meter.total.cost_usd == pytest.approx(settings.llm_cost_usd(1000, 200))


async def test_structured_repair_then_fail(settings):
    llm, client = make_llm(settings, [resp('{"name":"a"}'), resp('{"name":"a","count":3,"tags":["x"],"note":null}')])
    out = await llm.structured(Thing, purpose="t", system="s", user_content="u")
    assert out.count == 3
    repair_msgs = client.messages.calls[1]["messages"]
    assert repair_msgs[-1]["role"] == "user" and "failed validation" in repair_msgs[-1]["content"]

    llm, _ = make_llm(settings, [resp("not json"), resp("still not json")])
    with pytest.raises(LLMOutputError):
        await llm.structured(Thing, purpose="t", system="s", user_content="u")


async def test_refusal_raises(settings):
    llm, _ = make_llm(settings, [resp(stop="refusal")])
    with pytest.raises(LLMRefusal) as e:
        await llm.create(purpose="t", messages=[{"role": "user", "content": "x"}])
    assert e.value.category == "cyber"


async def test_pause_turn_continues(settings):
    llm, client = make_llm(settings, [resp(stop="pause_turn"), resp("done")])
    out = await llm.create(purpose="t", messages=[{"role": "user", "content": "x"}])
    assert out.text == "done"
    assert out.usage.input_tokens == 2000
    assert client.messages.calls[1]["messages"][-1]["role"] == "assistant"


async def test_cost_ceiling(settings):
    settings.agent_max_cost_usd = 0.001
    llm, _ = make_llm(settings, [resp("a", tokens=(10_000, 1000)), resp("b")])
    await llm.create(purpose="t", messages=[{"role": "user", "content": "x"}], job_id="job_ceiling")
    with pytest.raises(CostCeilingExceeded):
        await llm.create(purpose="t", messages=[{"role": "user", "content": "x"}], job_id="job_ceiling")


async def test_fallback_used_then_disabled_on_rejection(settings):
    llm, client = make_llm(settings, [], fallback=True, beta_responses=[resp("ok")])
    await llm.create(purpose="t", messages=[{"role": "user", "content": "x"}])
    call = client.beta.messages.calls[0]
    assert call["betas"] == [llm_mod.FALLBACK_BETA]
    assert call["fallbacks"] == "default"

    import httpx2

    response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    bad = anthropic.BadRequestError("fallbacks is not supported", response=response, body=None)
    llm, client = make_llm(settings, [resp("plain")], fallback=True, beta_responses=[bad])
    out = await llm.create(purpose="t", messages=[{"role": "user", "content": "x"}])
    assert out.text == "plain" and llm._fallback_enabled is False


def test_parse_web_search_results():
    content = [
        SimpleNamespace(type="server_tool_use", name="web_search"),
        SimpleNamespace(type="web_search_tool_result", content=[
            SimpleNamespace(url="https://pmkisan.gov.in/", title="PM-KISAN"),
            SimpleNamespace(url="https://pmkisan.gov.in/", title="dup"),
            SimpleNamespace(url="https://news.example.com/x", title="News"),
        ]),
        SimpleNamespace(type="web_search_tool_result", content=SimpleNamespace(error_code="max_uses_exceeded")),
        SimpleNamespace(type="text", text="see", citations=[
            SimpleNamespace(url="https://pmkisan.gov.in/", cited_text="Official portal of PM-KISAN"),
        ]),
    ]
    hits = parse_web_search_results(content)
    assert [h["url"] for h in hits] == ["https://pmkisan.gov.in/", "https://news.example.com/x"]
    assert hits[0]["snippet"] == "Official portal of PM-KISAN"


async def test_web_search_tool_definition(settings):
    content = [SimpleNamespace(type="web_search_tool_result", content=[SimpleNamespace(url="https://a.gov.in/", title="A")])]
    llm, client = make_llm(settings, [resp(content=content, searches=1)])
    hits = await llm.web_search("kushal yuva site:gov.in", max_results=5)
    tool = client.messages.calls[0]["tools"][0]
    assert tool["type"] == "web_search_20260209" and tool["name"] == "web_search"
    assert hits == [{"url": "https://a.gov.in/", "title": "A", "snippet": ""}]
