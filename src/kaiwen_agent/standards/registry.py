from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from kaiwen_agent.agents import VersionedReference
from kaiwen_agent.standards.models import (
    RequirementDefinition,
    ResolvedRequirement,
    StandardResolution,
    StandardsPack,
)


class StandardsRegistry:
    def __init__(self, packs: Iterable[StandardsPack] = ()) -> None:
        self._packs: dict[tuple[str, int], StandardsPack] = {}
        self._latest: dict[str, int] = {}
        for pack in packs:
            self.register(pack)

    def register(self, pack: StandardsPack) -> None:
        key = (pack.id, pack.version)
        if key in self._packs:
            raise ValueError(f"Standards pack already registered: {pack.id}@{pack.version}")
        self._packs[key] = pack
        self._latest[pack.id] = max(pack.version, self._latest.get(pack.id, 0))

    def get(self, pack_id: str, version: int | None = None) -> StandardsPack:
        selected = version if version is not None else self._latest.get(pack_id)
        if selected is None:
            raise KeyError(f"Unknown standards pack: {pack_id}")
        try:
            return self._packs[(pack_id, selected)]
        except KeyError as error:
            raise KeyError(f"Unknown standards pack: {pack_id}@{selected}") from error

    def resolve(self, reference: VersionedReference) -> StandardsPack:
        return self.get(reference.id, reference.version)

    def list(self) -> tuple[StandardsPack, ...]:
        return tuple(
            self._packs[key] for key in sorted(self._packs, key=lambda item: (item[0], item[1]))
        )


class StandardsResolver:
    def __init__(self, registry: StandardsRegistry) -> None:
        self.registry = registry

    def resolve(
        self,
        pack_refs: Sequence[VersionedReference],
        *,
        condition_facts: Mapping[str, bool] | None = None,
    ) -> StandardResolution:
        if len(pack_refs) != len(set(pack_refs)):
            raise ValueError("pack_refs must not contain duplicates")
        facts = condition_facts or {}
        packs = [self.registry.resolve(reference) for reference in pack_refs]
        grouped: dict[str, list[tuple[VersionedReference, RequirementDefinition]]] = {}
        prompt_refs: list[VersionedReference] = []
        for reference, pack in zip(pack_refs, packs, strict=True):
            prompt_refs.extend(pack.prompt_refs)
            for requirement in pack.requirements:
                grouped.setdefault(requirement.id, []).append((reference, requirement))

        resolved: list[ResolvedRequirement] = []
        conflicts: list[str] = []
        unresolved: list[str] = []
        for requirement_id in sorted(grouped):
            entries = grouped[requirement_id]
            definitions = [entry[1] for entry in entries]
            first = definitions[0]
            if any(definition != first for definition in definitions[1:]):
                conflicts.append(f"Conflicting requirement: {requirement_id}")
                continue
            applies: bool | None = True
            condition = first.applies_when
            if condition is not None:
                applies = facts.get(condition)
                if applies is None:
                    unresolved.append(requirement_id)
            resolved.append(
                ResolvedRequirement(
                    definition=first,
                    source_pack_refs=tuple(entry[0] for entry in entries),
                    applies=applies,
                )
            )
        unique_prompts = tuple(dict.fromkeys(prompt_refs))
        return StandardResolution(
            pack_refs=tuple(pack_refs),
            requirements=tuple(resolved),
            prompt_refs=unique_prompts,
            conflicts=tuple(conflicts),
            unresolved_conditions=tuple(unresolved),
        )
