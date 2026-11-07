from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from kaiwen_agent.tokens import estimate_tokens, truncate_to_tokens


class TokenCounter(Protocol):
    """Provider-aware text counting boundary used before a model request."""

    def count(self, text: str) -> int: ...

    def truncate(self, text: str, max_tokens: int) -> str: ...


class EstimatedTokenCounter:
    """Dependency-free fallback preserving the original four-character estimate."""

    def count(self, text: str) -> int:
        return estimate_tokens(text)

    def truncate(self, text: str, max_tokens: int) -> str:
        return truncate_to_tokens(text, max_tokens)


class CallableTokenCounter:
    """Adapter for a provider tokenizer without coupling core to its SDK."""

    def __init__(self, counter: Callable[[str], int]) -> None:
        self._counter = counter

    def count(self, text: str) -> int:
        count = self._counter(text)
        if count < 0:
            raise ValueError("Token counter must not return a negative value")
        return count

    def truncate(self, text: str, max_tokens: int) -> str:
        if max_tokens < 1:
            return ""
        if self.count(text) <= max_tokens:
            return text
        low = 0
        high = len(text)
        while low < high:
            midpoint = (low + high + 1) // 2
            if self.count(text[:midpoint]) <= max_tokens:
                low = midpoint
            else:
                high = midpoint - 1
        if low == 0:
            return ""
        suffix = "…"
        candidate = text[:low].rstrip()
        while candidate and self.count(candidate + suffix) > max_tokens:
            candidate = candidate[:-1].rstrip()
        return candidate + suffix if candidate else text[:low]
