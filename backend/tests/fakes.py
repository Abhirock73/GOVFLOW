"""Test doubles for the LLM and search layers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.discovery.search import SearchResult


class FakeLLM:
    """Returns canned structured outputs keyed by ``purpose``.

    A value may be a dict, a model instance, a list (consumed in order) or a
    callable taking the user content.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Any]] = []

    async def structured(self, model_cls: type[BaseModel], *, purpose: str, system: str,
                         user_content: Any, **_: Any) -> BaseModel:
        self.calls.append((purpose, user_content))
        r = self.responses[purpose]
        if isinstance(r, list):
            r = r.pop(0)
        if callable(r) and not isinstance(r, BaseModel):
            r = r(user_content)
        return r if isinstance(r, BaseModel) else model_cls.model_validate(r)

    def user_content_for(self, purpose: str) -> str:
        return next(str(c) for p, c in self.calls if p == purpose)


class FakeSearch:
    name = "fake"

    def __init__(self, results: dict[str, list[tuple[str, str]]] | Callable[[str], list[tuple[str, str]]]) -> None:
        self.results = results
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        self.queries.append(query)
        hits = self.results(query) if callable(self.results) else self.results.get(query, self.results.get("*", []))
        return [SearchResult(title, url, "", self.name, i) for i, (title, url) in enumerate(hits)][:max_results]
