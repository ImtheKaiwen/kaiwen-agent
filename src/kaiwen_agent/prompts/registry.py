from __future__ import annotations

import string
from datetime import datetime
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.types import utc_now


class PromptDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    template: str = Field(min_length=1)
    variables: frozenset[str] = Field(default_factory=frozenset)
    description: str = ""

    @property
    def checksum(self) -> str:
        return sha256(self.template.encode()).hexdigest()


class RenderedPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_id: str
    prompt_version: int
    checksum: str
    text: str


class PromptRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    prompt_id: str
    prompt_version: int
    prompt_checksum: str
    model: str = Field(min_length=1)
    toolset_version: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    memory_policy_version: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class PromptRegistry:
    def __init__(self) -> None:
        self._definitions: dict[tuple[str, int], PromptDefinition] = {}
        self._latest: dict[str, int] = {}
        self._runs: dict[str, PromptRunRecord] = {}

    def register(self, definition: PromptDefinition) -> None:
        key = (definition.id, definition.version)
        if key in self._definitions:
            raise ValueError(f"Prompt version already registered: {key}")
        declared = _template_variables(definition.template)
        if declared != definition.variables:
            raise ValueError(f"Prompt variables do not match template; expected {sorted(declared)}")
        self._definitions[key] = definition
        self._latest[definition.id] = max(definition.version, self._latest.get(definition.id, 0))

    def get(self, prompt_id: str, version: int | None = None) -> PromptDefinition:
        selected = version if version is not None else self._latest.get(prompt_id)
        if selected is None:
            raise KeyError(f"Unknown prompt: {prompt_id}")
        try:
            return self._definitions[(prompt_id, selected)]
        except KeyError as error:
            raise KeyError(f"Unknown prompt version: {prompt_id}@{selected}") from error

    def render(
        self, prompt_id: str, values: dict[str, str], *, version: int | None = None
    ) -> RenderedPrompt:
        definition = self.get(prompt_id, version)
        if set(values) != set(definition.variables):
            raise ValueError("Prompt values must match the declared variables exactly")
        return RenderedPrompt(
            prompt_id=definition.id,
            prompt_version=definition.version,
            checksum=definition.checksum,
            text=definition.template.format_map(values),
        )

    def record_run(self, record: PromptRunRecord) -> None:
        if record.run_id in self._runs:
            raise ValueError(f"Prompt run already recorded: {record.run_id}")
        definition = self.get(record.prompt_id, record.prompt_version)
        if record.prompt_checksum != definition.checksum:
            raise ValueError("Prompt run checksum does not match the registered version")
        self._runs[record.run_id] = record

    def get_run(self, run_id: str) -> PromptRunRecord:
        try:
            return self._runs[run_id]
        except KeyError as error:
            raise KeyError(f"Unknown prompt run: {run_id}") from error


def _template_variables(template: str) -> frozenset[str]:
    variables: set[str] = set()
    for _, field, format_spec, conversion in string.Formatter().parse(template):
        if field is None:
            continue
        if not field or "." in field or "[" in field or format_spec or conversion:
            raise ValueError("Prompt variables must be simple names without formatting")
        variables.add(field)
    return frozenset(variables)
