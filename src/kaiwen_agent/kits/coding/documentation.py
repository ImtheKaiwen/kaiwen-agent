from __future__ import annotations

from enum import StrEnum
from importlib.resources import files

from pydantic import BaseModel, ConfigDict

from kaiwen_agent.kits.coding.discovery import CodingProjectSnapshot


class DocumentationTier(StrEnum):
    S = "single_change"
    M = "small_project"
    L = "substantial_project"


class DocumentationAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    action: str
    reason: str
    template: str | None = None


class DocumentationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tier: DocumentationTier
    actions: tuple[DocumentationAction, ...]


def classify_documentation_tier(
    *,
    estimated_changed_files: int,
    architecture_change: bool = False,
    new_project: bool = False,
) -> DocumentationTier:
    if estimated_changed_files < 1:
        raise ValueError("estimated_changed_files must be positive")
    if architecture_change or (new_project and estimated_changed_files >= 10):
        return DocumentationTier.L
    if new_project or estimated_changed_files > 2:
        return DocumentationTier.M
    return DocumentationTier.S


def propose_documentation(
    snapshot: CodingProjectSnapshot,
    *,
    tier: DocumentationTier,
) -> DocumentationProposal:
    existing = {path.casefold() for path in snapshot.documentation_paths}
    if tier == DocumentationTier.S:
        return DocumentationProposal(tier=tier, actions=())
    desired = ["README.md", "docs/PLAN.md", "docs/QUALITY.md"]
    if tier == DocumentationTier.L:
        desired.extend(("docs/ARCHITECTURE.md", "docs/DECISIONS.md"))
        if snapshot.condition_facts.get("has_ui") is True:
            desired.append("docs/DESIGN.md")
    actions = tuple(
        DocumentationAction(
            path=path,
            action="update" if path.casefold() in existing else "create",
            reason="Required by the selected proportionate documentation tier.",
            template=None if path.casefold() in existing else _template_for(path),
        )
        for path in desired
    )
    return DocumentationProposal(tier=tier, actions=actions)


def _template_for(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    resource = files("kaiwen_agent.kits.coding.templates.docs").joinpath(name)
    return resource.read_text(encoding="utf-8").strip()
