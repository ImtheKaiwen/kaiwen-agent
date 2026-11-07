from __future__ import annotations


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def truncate_to_tokens(text: str, tokens: int) -> str:
    if tokens < 1:
        return ""
    if estimate_tokens(text) <= tokens:
        return text
    character_limit = max(1, tokens * 4)
    if character_limit <= 1:
        return text[:character_limit]
    return text[: character_limit - 1].rstrip() + "…"
