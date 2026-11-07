from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.context import AgentContext
from kaiwen_agent.integrations import WorkspaceFilesystem, WorkspacePathError
from kaiwen_agent.tools.definition import ToolDefinition


class ProjectManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str


class ProjectSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    root: str
    project_types: tuple[str, ...]
    languages: tuple[str, ...]
    frameworks: tuple[str, ...]
    package_managers: tuple[str, ...]
    manifests: tuple[ProjectManifest, ...]
    condition_facts: dict[str, bool | None]
    suggested_checks: tuple[str, ...]


class ProjectDetectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1, max_length=100)


class ProjectDetector:
    """Deterministic manifest-based discovery; suggestions do not authorize commands."""

    _KNOWN = (
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "tsconfig.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "index.html",
        ".github/workflows/ci.yml",
        ".github/workflows/ci.yaml",
    )

    def __init__(self, filesystem: WorkspaceFilesystem) -> None:
        self.filesystem = filesystem

    def detect(self, root: str) -> ProjectSnapshot:
        if root not in self.filesystem.roots:
            raise WorkspacePathError("Project root is not allowlisted")
        contents: dict[str, str] = {}
        manifests: list[ProjectManifest] = []
        for path in self._KNOWN:
            try:
                content = self.filesystem.read_text(root, path)
            except WorkspacePathError:
                continue
            except (UnicodeDecodeError, ValueError):
                continue
            contents[path] = content
            manifests.append(
                ProjectManifest(path=path, sha256=sha256(content.encode()).hexdigest())
            )

        types: set[str] = set()
        languages: set[str] = set()
        frameworks: set[str] = set()
        package_managers: set[str] = set()
        checks: list[str] = []
        if "pyproject.toml" in contents or "requirements.txt" in contents:
            types.add("python")
            languages.add("python")
            package_managers.add("python")
            checks.append("python.tests")
        package = _package_json(contents.get("package.json"))
        if package is not None:
            types.add("node")
            languages.add("typescript" if "tsconfig.json" in contents else "javascript")
            package_managers.add(_node_package_manager(contents))
            dependencies = {
                *package.get("dependencies", {}),
                *package.get("devDependencies", {}),
            }
            frameworks.update(
                name
                for name in ("next", "react", "vue", "svelte", "@angular/core")
                if name in dependencies
            )
            scripts = package.get("scripts", {})
            if isinstance(scripts, dict):
                for script in ("test", "lint", "typecheck", "build"):
                    if script in scripts:
                        checks.append(f"npm.{script}")
        if "index.html" in contents and "package.json" not in contents:
            types.add("static_web")
            languages.update({"html", "css", "javascript"})
            checks.append("static.validate")
        if "Cargo.toml" in contents:
            types.add("rust")
            languages.add("rust")
            package_managers.add("cargo")
            checks.append("cargo.test")
        if "go.mod" in contents:
            types.add("go")
            languages.add("go")
            package_managers.add("go")
            checks.append("go.test")
        if "pom.xml" in contents or "build.gradle" in contents:
            types.add("java")
            languages.add("java")
            package_managers.add("maven" if "pom.xml" in contents else "gradle")
            checks.append("java.test")

        ui_frameworks = {"next", "react", "vue", "svelte", "@angular/core"}
        has_ui = bool({"static_web"} & types or frameworks & ui_frameworks)
        has_ci = any(path.startswith(".github/workflows/") for path in contents)
        return ProjectSnapshot(
            workspace_id=self.filesystem.workspace_id,
            root=root,
            project_types=tuple(sorted(types)) or ("unknown",),
            languages=tuple(sorted(languages)),
            frameworks=tuple(sorted(frameworks)),
            package_managers=tuple(sorted(package_managers)),
            manifests=tuple(sorted(manifests, key=lambda item: item.path)),
            condition_facts={
                "has_ui": has_ui,
                "uses_ci": has_ci,
                "has_executable_tests": bool(checks),
                "is_existing_repo": bool(manifests),
                "has_database": None,
                "has_auth": None,
                "has_network": None,
                "is_multi_module": None,
                "performance_critical": None,
                "requires_documentation": None,
            },
            suggested_checks=tuple(dict.fromkeys(checks)),
        )


def project_detect_tool(detector: ProjectDetector) -> ToolDefinition:
    async def detect(context: AgentContext, arguments: BaseModel) -> Any:
        if not isinstance(arguments, ProjectDetectInput):
            raise TypeError("Project detection arguments were not validated")
        detector.filesystem.require_context(context)
        return detector.detect(arguments.root).model_dump(mode="json")

    return ToolDefinition(
        name="project.detect",
        description="Detect bounded project facts from known workspace manifests.",
        input_model=ProjectDetectInput,
        handler=detect,
        permissions=frozenset({"workspace.read"}),
    )


def _package_json(content: str | None) -> dict[str, Any] | None:
    if content is None:
        return None
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _node_package_manager(contents: dict[str, str]) -> str:
    if "pnpm-lock.yaml" in contents:
        return "pnpm"
    if "yarn.lock" in contents:
        return "yarn"
    return "npm"
