"""Pluggable web search used only to find and verify official portals.

Providers:
  * ``anthropic`` (default): Claude's server-side web search tool via llm.py
  * ``tavily``: Tavily Search API (``TAVILY_API_KEY``)
  * ``brave``: Brave Search API (``BRAVE_API_KEY``)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from ..config import Settings, get_settings
from ..llm import LLM, get_llm

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    provider: str
    rank: int = 0


class SearchError(RuntimeError):
    pass


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...


class AnthropicWebSearch:
    name = "anthropic"

    def __init__(self, llm: LLM, job_id: str | None = None) -> None:
        self._llm = llm
        self._job_id = job_id

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        hits = await self._llm.web_search(query, max_results=max_results, job_id=self._job_id)
        return [SearchResult(h["title"], h["url"], h["snippet"], self.name, i) for i, h in enumerate(hits)]


class TavilySearch:
    name = "tavily"
    endpoint = "https://api.tavily.com/search"

    def __init__(self, api_key: str, timeout: float, client: httpx.AsyncClient | None = None) -> None:
        self._key = api_key
        self._timeout = timeout
        self._client = client

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        payload = {"query": query, "max_results": max_results, "search_depth": "basic", "include_answer": False}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        data = await _request(self._client, "POST", self.endpoint, self._timeout, json=payload, headers=headers)
        return [
            SearchResult(r.get("title", ""), r["url"], (r.get("content") or "")[:400], self.name, i)
            for i, r in enumerate(data.get("results", []))
            if r.get("url")
        ][:max_results]


class BraveSearch:
    name = "brave"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, timeout: float, client: httpx.AsyncClient | None = None) -> None:
        self._key = api_key
        self._timeout = timeout
        self._client = client

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        headers = {"X-Subscription-Token": self._key, "Accept": "application/json"}
        params = {"q": query, "count": min(max_results, 20)}
        data = await _request(self._client, "GET", self.endpoint, self._timeout, params=params, headers=headers)
        results = (data.get("web") or {}).get("results", [])
        return [
            SearchResult(r.get("title", ""), r["url"], (r.get("description") or "")[:400], self.name, i)
            for i, r in enumerate(results)
            if r.get("url")
        ][:max_results]


async def _request(client: httpx.AsyncClient | None, method: str, url: str, timeout: float, **kw) -> dict:
    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        for attempt in range(3):
            try:
                resp = await client.request(method, url, **kw)
            except httpx.TransportError as e:
                if attempt == 2:
                    raise SearchError(f"search request failed: {e}") from e
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == 2:
                    raise SearchError(f"search provider returned HTTP {resp.status_code}")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise SearchError(f"search provider returned HTTP {resp.status_code}")
            return resp.json()
        raise SearchError("search failed")
    finally:
        if owns:
            await client.aclose()


def get_search_provider(settings: Settings | None = None, llm: LLM | None = None,
                        job_id: str | None = None) -> SearchProvider:
    settings = settings or get_settings()
    if settings.search_provider == "tavily":
        if not settings.tavily_api_key:
            raise SearchError("SEARCH_PROVIDER=tavily but TAVILY_API_KEY is not set")
        return TavilySearch(settings.tavily_api_key.get_secret_value(), settings.http_timeout_seconds)
    if settings.search_provider == "brave":
        if not settings.brave_api_key:
            raise SearchError("SEARCH_PROVIDER=brave but BRAVE_API_KEY is not set")
        return BraveSearch(settings.brave_api_key.get_secret_value(), settings.http_timeout_seconds)
    return AnthropicWebSearch(llm or get_llm(), job_id)
