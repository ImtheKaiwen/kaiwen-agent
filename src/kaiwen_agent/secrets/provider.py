from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, SecretStr

_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")


class SecretNotFoundError(LookupError):
    pass


class SecretValue(BaseModel):
    """Opaque value whose repr and serialization never reveal the plaintext."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    value: SecretStr

    def reveal(self) -> str:
        return self.value.get_secret_value()


class SecretProvider(Protocol):
    async def get(self, key: str) -> SecretValue: ...


class EnvironmentSecretProvider:
    """Server-side environment adapter with an explicit key allowlist."""

    def __init__(
        self,
        *,
        allowed_keys: frozenset[str] | set[str],
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if not allowed_keys:
            raise ValueError("allowed_keys cannot be empty")
        invalid = [key for key in allowed_keys if not _KEY_PATTERN.fullmatch(key)]
        if invalid:
            raise ValueError(f"Invalid environment secret keys: {sorted(invalid)}")
        self.allowed_keys = frozenset(allowed_keys)
        self.environ = environ if environ is not None else os.environ

    async def get(self, key: str) -> SecretValue:
        if key not in self.allowed_keys:
            raise PermissionError("Secret key is not allowlisted")
        value = self.environ.get(key)
        if value is None or not value:
            raise SecretNotFoundError(f"Secret is not configured: {key}")
        return SecretValue(key=key, value=SecretStr(value))
