"""The single gateway for every Claude call in GovFlow.

Responsibilities:

* one configured ``AsyncAnthropic`` client (timeouts, SDK retries);
* structured JSON output through ``output_config.format`` validated against a
  Pydantic model, with one repair retry on invalid output;
* the server-side refusal fallback (on by default, Claude API only);
* ``pause_turn`` continuation for server tools such as web search;
* token / cost accounting per call and per job, with a hard per-job ceiling;
* logging that never contains prompt or response text (only counts).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anthropic
import openai
from pydantic import BaseModel, ValidationError

from .config import Settings, get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

WEB_SEARCH_TOOL_TYPE = "web_search_20260209"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_PAUSE_CONTINUATIONS = 4

# JSON-schema keywords that strict structured output may not accept. Pydantic
# still enforces them when validating the reply.
_STRIP_KEYS = {
    "title", "default", "examples", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "pattern", "format", "minItems", "maxItems", "uniqueItems",
}


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    def __init__(self, category: str | None, explanation: str | None):
        super().__init__(f"model declined the request (category={category})")
        self.category = category
        self.explanation = explanation


class LLMOutputError(LLMError):
    """The model's structured output failed validation twice."""


class CostCeilingExceeded(LLMError):
    def __init__(self, job_id: str, spent: float, ceiling: float):
        super().__init__(f"job {job_id} reached its cost ceiling (${spent:.4f} of ${ceiling:.2f})")
        self.job_id = job_id
        self.spent = spent
        self.ceiling = ceiling


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0
    cost_usd: float = 0.0

    def __iadd__(self, other: "Usage") -> "Usage":
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.web_searches += other.web_searches
        self.cost_usd += other.cost_usd
        return self


@dataclass
class CostMeter:
    job_id: str
    ceiling_usd: float
    total: Usage = field(default_factory=Usage)
    calls: int = 0

    def check(self) -> None:
        if self.total.cost_usd >= self.ceiling_usd:
            raise CostCeilingExceeded(self.job_id, self.total.cost_usd, self.ceiling_usd)

    def add(self, usage: Usage) -> None:
        self.total += usage
        self.calls += 1


_meters: dict[str, CostMeter] = {}
_meters_lock = threading.Lock()


def get_meter(job_id: str, settings: Settings | None = None) -> CostMeter:
    settings = settings or get_settings()
    with _meters_lock:
        meter = _meters.get(job_id)
        if meter is None:
            meter = _meters[job_id] = CostMeter(job_id, settings.agent_max_cost_usd)
        return meter


def restore_meter(job_id: str, spent_usd: float, settings: Settings | None = None) -> CostMeter:
    """Re-seed a job's spend after a restart (from the checkpointed state)."""
    meter = get_meter(job_id, settings)
    meter.total.cost_usd = max(meter.total.cost_usd, spent_usd)
    return meter


def drop_meter(job_id: str) -> None:
    with _meters_lock:
        _meters.pop(job_id, None)


# ---------------------------------------------------------------------------
# Structured output schema
# ---------------------------------------------------------------------------


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema adapted for strict structured output.

    Every object gets ``additionalProperties: false`` and lists all of its
    properties as required (optional fields are already nullable unions).
    Models used here must not contain free-form ``dict`` fields.
    """
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k in list(node):
                if k in _STRIP_KEYS:
                    del node[k]
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    text: str
    content: list[Any]
    stop_reason: str | None
    usage: Usage
    model: str
    request_id: str | None = None


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _usage_of(resp: Any, settings: Settings) -> Usage:
    u = _attr(resp, "usage")
    stu = _attr(u, "server_tool_use")
    usage = Usage(
        input_tokens=_attr(u, "input_tokens", 0) or 0,
        output_tokens=_attr(u, "output_tokens", 0) or 0,
        cache_read_tokens=_attr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=_attr(u, "cache_creation_input_tokens", 0) or 0,
        web_searches=_attr(stu, "web_search_requests", 0) or 0,
    )
    usage.cost_usd = settings.llm_cost_usd(
        usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
        usage.cache_write_tokens, usage.web_searches,
    )
    return usage


def _text_of(content: Sequence[Any]) -> str:
    return "".join(_attr(b, "text", "") for b in content if _attr(b, "type") == "text")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LLM:
    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.provider = self.settings.effective_provider
        self._openai_client = None

        if client is None:
            if self.provider == "gemini":
                g_key = self.settings.gemini_api_key.get_secret_value() if self.settings.gemini_api_key else ""
                self._openai_client = openai.AsyncOpenAI(
                    api_key=g_key,
                    base_url=self.settings.gemini_base_url,
                    timeout=self.settings.llm_timeout_seconds,
                    max_retries=self.settings.llm_max_retries,
                )
                client = None
            else:
                key = self.settings.anthropic_api_key
                client = anthropic.AsyncAnthropic(
                    api_key=key.get_secret_value() if key else None,
                    timeout=self.settings.llm_timeout_seconds,
                    max_retries=self.settings.llm_max_retries,
                )
        self._client = client
        self._fallback_enabled = self.settings.llm_refusal_fallback

    @property
    def model(self) -> str:
        if self._openai_client is not None:
            return self.settings.gemini_model
        return self.settings.anthropic_model

    async def create(
        self,
        *,
        purpose: str,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        output_schema: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        job_id: str | None = None,
    ) -> LLMResponse:
        """One logical call, including any ``pause_turn`` continuations."""
        if self._openai_client is not None:
            return await self._create_gemini(
                purpose=purpose,
                messages=messages,
                system=system,
                tools=tools,
                output_schema=output_schema,
                max_tokens=max_tokens,
                effort=effort,
                job_id=job_id,
            )

        meter = get_meter(job_id, self.settings) if job_id else None
        if meter:
            meter.check()

        output_config: dict[str, Any] = {"effort": effort or self.settings.llm_effort}
        if output_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": strict_json_schema(output_schema)}
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
            "messages": list(messages),
            "output_config": output_config,
        }
        if system is not None:
            params["system"] = system
        if tools:
            params["tools"] = tools

        total = Usage()
        started = time.monotonic()
        resp = None
        for _ in range(MAX_PAUSE_CONTINUATIONS + 1):
            resp = await self._send(params)
            total += _usage_of(resp, self.settings)
            if _attr(resp, "stop_reason") != "pause_turn":
                break
            # A server tool hit its turn limit; resend so it can continue.
            params["messages"] = [*params["messages"], {"role": "assistant", "content": _attr(resp, "content")}]
        assert resp is not None

        if meter:
            meter.add(total)
        logger.info(
            "llm purpose=%s model=%s stop=%s in=%d out=%d cache_r=%d searches=%d cost=$%.4f ms=%d job=%s%s",
            purpose, _attr(resp, "model", self.model), _attr(resp, "stop_reason"), total.input_tokens,
            total.output_tokens, total.cache_read_tokens, total.web_searches, total.cost_usd,
            int((time.monotonic() - started) * 1000), job_id or "-",
            f" job_total=${meter.total.cost_usd:.4f}" if meter else "",
        )

        stop = _attr(resp, "stop_reason")
        if stop == "refusal":
            details = _attr(resp, "stop_details")
            raise LLMRefusal(_attr(details, "category"), _attr(details, "explanation"))
        content = list(_attr(resp, "content", []) or [])
        return LLMResponse(
            text=_text_of(content),
            content=content,
            stop_reason=stop,
            usage=total,
            model=_attr(resp, "model", self.model),
            request_id=_attr(resp, "_request_id"),
        )

    async def _create_gemini(
        self,
        *,
        purpose: str,
        messages: list[dict[str, Any]],
        system: str | list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        output_schema: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        job_id: str | None = None,
    ) -> LLMResponse:
        meter = get_meter(job_id, self.settings) if job_id else None
        if meter:
            meter.check()

        started = time.monotonic()
        openai_messages: list[dict[str, Any]] = []

        system_text = ""
        if isinstance(system, str):
            system_text = system
        elif isinstance(system, list):
            system_text = "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
        if output_schema is not None:
            schema_json = json.dumps(strict_json_schema(output_schema))
            system_text = (system_text + f"\n\nReturn ONLY a JSON object conforming exactly to this JSON schema:\n{schema_json}").strip()

        if system_text:
            openai_messages.append({"role": "system", "content": system_text})

        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if isinstance(content, str):
                openai_messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, dict):
                        b_type = b.get("type", "")
                        if b_type == "text":
                            parts.append({"type": "text", "text": b.get("text", "")})
                        elif b_type == "image":
                            src = b.get("source", {})
                            mtype = src.get("media_type", "image/png")
                            data = src.get("data", "")
                            parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{mtype};base64,{data}"}
                            })
                openai_messages.append({"role": role, "content": parts if parts else ""})

        openai_tools = None
        if tools:
            openai_tools = []
            for t in tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    }
                })

        last_exc = None
        for attempt in range(self.settings.llm_max_retries + 2):
            try:
                resp = await self._openai_client.chat.completions.create(
                    model=self.model,
                    messages=openai_messages,
                    tools=openai_tools,
                    response_format={"type": "json_object"} if output_schema else None,
                    max_tokens=max_tokens or self.settings.llm_max_tokens,
                )
                break
            except Exception as e:
                last_exc = e
                err_str = str(e).lower()
                if ("503" in err_str or "429" in err_str or "demand" in err_str or "quota" in err_str) and attempt < self.settings.llm_max_retries + 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise
        else:
            raise LLMError(f"Gemini API request failed after retries: {last_exc}") from last_exc

        choice = resp.choices[0]
        text_content = choice.message.content or ""
        content_blocks: list[Any] = []
        if text_content:
            content_blocks.append({"type": "text", "text": text_content})

        has_tool_calls = bool(choice.message.tool_calls)
        if has_tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": args,
                })

        stop_reason = "tool_use" if has_tool_calls else (choice.finish_reason or "end_turn")
        in_tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
        out_tokens = getattr(resp.usage, "completion_tokens", 0) or 0
        usage = Usage(input_tokens=in_tokens, output_tokens=out_tokens, cost_usd=0.0)

        if meter:
            meter.add(usage)
        logger.info(
            "llm provider=gemini purpose=%s model=%s stop=%s in=%d out=%d ms=%d job=%s",
            purpose, resp.model or self.model, stop_reason, in_tokens, out_tokens,
            int((time.monotonic() - started) * 1000), job_id or "-",
        )

        return LLMResponse(
            text=text_content,
            content=content_blocks,
            stop_reason=stop_reason,
            usage=usage,
            model=resp.model or self.model,
            request_id=resp.id,
        )

    async def _send(self, params: dict[str, Any]) -> Any:
        if isinstance(self._client, anthropic.AsyncAnthropic):
            if not self.settings.anthropic_api_key or not self.settings.anthropic_api_key.get_secret_value().strip():
                raise LLMError("ANTHROPIC_API_KEY is not set. Please configure ANTHROPIC_API_KEY in your .env file.")
        if self._fallback_enabled:
            try:
                return await self._client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e).lower():
                    raise
                # Not supported on this endpoint/account: disable for this process.
                logger.warning("refusal fallback unavailable; continuing without it")
                self._fallback_enabled = False
        return await self._client.messages.create(**params)

    async def structured(
        self,
        model_cls: type[T],
        *,
        purpose: str,
        system: str,
        user_content: str | list[dict[str, Any]],
        max_tokens: int | None = None,
        effort: str | None = None,
        job_id: str | None = None,
    ) -> T:
        """Ask for JSON matching ``model_cls`` and return the validated object."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        last_error: Exception | None = None
        for attempt in range(2):
            resp = await self.create(
                purpose=purpose if attempt == 0 else f"{purpose}:repair",
                system=system,
                messages=messages,
                output_schema=model_cls,
                max_tokens=max_tokens,
                effort=effort,
                job_id=job_id,
            )
            raw = (resp.text or "").strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)
            try:
                return model_cls.model_validate_json(raw)
            except ValidationError as e:
                last_error = e
                if resp.stop_reason == "max_tokens":
                    max_tokens = min((max_tokens or self.settings.llm_max_tokens) * 2, 32000)
                messages = [
                    *messages,
                    {"role": "assistant", "content": resp.text or "{}"},
                    {
                        "role": "user",
                        "content": "That JSON failed validation:\n"
                        + _short_errors(e)
                        + "\nReturn the corrected JSON only.",
                    },
                ]
        raise LLMOutputError(f"{purpose}: invalid structured output") from last_error

    async def web_search(
        self,
        query: str,
        *,
        max_results: int = 8,
        allowed_domains: list[str] | None = None,
        job_id: str | None = None,
    ) -> list[dict[str, str]]:
        """Run the server-side web search tool and return ``{url,title,snippet}``."""
        if self._openai_client is not None:
            return []
        tool: dict[str, Any] = {"type": WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": 2}
        if allowed_domains:
            tool["allowed_domains"] = allowed_domains
        resp = await self.create(
            purpose="web_search",
            system=(
                "You run web searches for a citizen-services assistant. Search for exactly the query "
                "given, then reply with one short line listing the most relevant result URLs."
            ),
            messages=[{"role": "user", "content": f"Search the web for: {query}"}],
            tools=[tool],
            max_tokens=2048,
            effort="low",
            job_id=job_id,
        )
        return parse_web_search_results(resp.content)[:max_results]


def parse_web_search_results(content: Sequence[Any]) -> list[dict[str, str]]:
    """Collect search hits from ``web_search_tool_result`` blocks, with
    snippets taken from any citations in the text blocks."""
    snippets: dict[str, str] = {}
    for block in content:
        if _attr(block, "type") != "text":
            continue
        for c in _attr(block, "citations") or []:
            url = _attr(c, "url")
            cited = _attr(c, "cited_text")
            if url and cited and url not in snippets:
                snippets[url] = cited[:400]

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in content:
        if _attr(block, "type") != "web_search_tool_result":
            continue
        items = _attr(block, "content")
        if not isinstance(items, list):  # error object, e.g. max_uses_exceeded
            logger.warning("web search error: %s", _attr(items, "error_code"))
            continue
        for item in items:
            url = _attr(item, "url")
            if not url or url in seen:
                continue
            seen.add(url)
            results.append({"url": url, "title": _attr(item, "title") or "", "snippet": snippets.get(url, "")})
    return results


def _short_errors(e: ValidationError) -> str:
    return json.dumps(
        [{"loc": ".".join(map(str, err["loc"])), "msg": err["msg"]} for err in e.errors()[:10]],
        ensure_ascii=False,
    )


_default: LLM | None = None


def get_llm() -> LLM:
    global _default
    if _default is None:
        _default = LLM()
    return _default
