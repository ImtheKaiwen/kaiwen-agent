from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.artifacts import ArtifactContext, ArtifactCreate, ArtifactManager, ArtifactType
from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.toolkits import ProjectDetector, ProjectSnapshot


class DiscoverySource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str
    kind: str


class CodingProjectSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    workspace_id: str
    root: str
    project: ProjectSnapshot
    file_count: int = Field(ge=0)
    truncated: bool
    top_level_paths: tuple[str, ...]
    documentation_paths: tuple[str, ...]
    instruction_paths: tuple[str, ...]
    design_system_indicators: tuple[str, ...]
    condition_facts: dict[str, bool | None]
    sources: tuple[DiscoverySource, ...]
    fingerprint: str


class ProjectDiscovery:
    """Bounded deterministic discovery over a trusted WorkspaceFilesystem."""

    def __init__(
        self,
        filesystem: WorkspaceFilesystem,
        *,
        max_files: int = 2_000,
        max_depth: int = 8,
    ) -> None:
        if max_files < 1 or max_depth < 1:
            raise ValueError("Discovery limits must be positive")
        self.filesystem = filesystem
        self.detector = ProjectDetector(filesystem)
        self.max_files = max_files
        self.max_depth = max_depth

    def discover(self, root: str) -> CodingProjectSnapshot:
        project = self.detector.detect(root)
        listing = self.filesystem.list_files(
            root,
            ".",
            max_depth=self.max_depth,
            max_results=self.max_files,
        )
        entries = tuple(listing["entries"])
        paths = tuple(str(entry["path"]) for entry in entries)
        files = tuple(str(entry["path"]) for entry in entries if entry.get("type") == "file")
        top_level = tuple(sorted({path.split("/", 1)[0] for path in paths if path != "."}))
        docs = tuple(
            path
            for path in files
            if path.casefold() == "readme.md" or path.casefold().startswith("docs/")
        )
        instructions = tuple(
            path
            for path in files
            if path.casefold() in {"agents.md", "contributing.md"}
            or path.casefold().endswith("/agents.md")
        )
        design = tuple(sorted(_design_indicators(files, project)))
        facts = dict(project.condition_facts)
        facts.update(
            {
                "has_database": _has_any(
                    files,
                    ("alembic", "migrations", "prisma", "schema.sql", "database"),
                ),
                "has_auth": _has_any(files, ("auth", "oauth", "session", "passport")),
                "has_network": bool(
                    set(project.frameworks) & {"next", "@angular/core"}
                    or _has_any(files, ("api/", "routes/", "router", "openapi"))
                ),
                "is_multi_module": _is_multi_module(files),
                "requires_documentation": len(files) >= 10 or len(project.project_types) > 1,
                "performance_critical": None,
            }
        )
        sources = tuple(
            DiscoverySource(path=item.path, sha256=item.sha256, kind="manifest")
            for item in project.manifests
        )
        material: dict[str, Any] = {
            "workspace_id": self.filesystem.workspace_id,
            "root": root,
            "project": project.model_dump(mode="json"),
            "files": files,
            "truncated": bool(listing["truncated"]),
            "facts": facts,
            "sources": [source.model_dump(mode="json") for source in sources],
        }
        fingerprint = sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return CodingProjectSnapshot(
            workspace_id=self.filesystem.workspace_id,
            root=root,
            project=project,
            file_count=len(files),
            truncated=bool(listing["truncated"]),
            top_level_paths=top_level,
            documentation_paths=docs,
            instruction_paths=instructions,
            design_system_indicators=design,
            condition_facts=facts,
            sources=sources,
            fingerprint=fingerprint,
        )

    def is_current(self, snapshot: CodingProjectSnapshot) -> bool:
        if snapshot.workspace_id != self.filesystem.workspace_id:
            return False
        return self.discover(snapshot.root).fingerprint == snapshot.fingerprint

    async def persist(
        self,
        snapshot: CodingProjectSnapshot,
        *,
        artifacts: ArtifactManager,
        context: ArtifactContext,
    ) -> str:
        metadata = await artifacts.create(
            context,
            ArtifactCreate(
                type=ArtifactType.ANALYSIS,
                mime_type="application/json",
                content=snapshot.model_dump_json().encode(),
                labels={
                    "artifact_kind": "coding_project_snapshot",
                    "workspace_id": snapshot.workspace_id,
                    "input_fingerprint": snapshot.fingerprint,
                },
            ),
        )
        return metadata.id


def _has_any(paths: tuple[str, ...], indicators: tuple[str, ...]) -> bool:
    folded = tuple(path.casefold() for path in paths)
    return any(indicator in path for path in folded for indicator in indicators)


def _is_multi_module(paths: tuple[str, ...]) -> bool:
    source_roots = {
        path.split("/", 2)[1]
        for path in paths
        if path.startswith(("src/", "app/", "packages/")) and path.count("/") >= 2
    }
    return len(source_roots) >= 2


def _design_indicators(files: tuple[str, ...], project: ProjectSnapshot) -> set[str]:
    indicators: set[str] = set()
    for path in files:
        folded = path.casefold()
        if "tailwind" in folded:
            indicators.add("tailwind")
        if "tokens" in folded or "theme" in folded:
            indicators.add("design_tokens")
        if folded.startswith(("components/ui/", "src/components/ui/")):
            indicators.add("shared_ui_components")
    if set(project.frameworks) & {"next", "react", "vue", "svelte", "@angular/core"}:
        indicators.add("component_framework")
    return indicators
