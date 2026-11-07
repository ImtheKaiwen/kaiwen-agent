"""Release consistency and distribution checks used by CI and the release workflow.

Usage:
    python scripts/check_release.py metadata
    python scripts/check_release.py dist <directory>

The script uses only the standard library. Python and npm versions are intentionally
independent; only relationships with a documented compatibility requirement are checked.
"""

from __future__ import annotations

import json
import re
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "src" / "kaiwen_agent"
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
FORBIDDEN_SDIST_PREFIXES = (
    ".github/",
    "docs/",
    "examples/",
    "node_modules/",
    "packages/",
    "scripts/",
)


def python_project() -> dict[str, object]:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    assert isinstance(project, dict)
    return project


def check_metadata() -> list[str]:
    errors: list[str] = []
    project = python_project()
    version = str(project["version"])
    if not re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?", version):
        errors.append(f"pyproject version is not a supported PEP 440 form: {version}")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## {re.escape(version)}\b", changelog, flags=re.MULTILINE):
        errors.append(f"CHANGELOG.md has no heading for Python version {version}")

    protocol = json.loads((ROOT / "packages/protocol/package.json").read_text(encoding="utf-8"))
    ui = json.loads((ROOT / "packages/ui-agent/package.json").read_text(encoding="utf-8"))
    for manifest in (protocol, ui):
        if not SEMVER.fullmatch(manifest["version"]):
            errors.append(f"{manifest['name']} version is not semver: {manifest['version']}")
    dependency = ui.get("dependencies", {}).get(protocol["name"])
    if dependency != protocol["version"]:
        errors.append(
            f"{ui['name']} must depend on the exact {protocol['name']} version "
            f"{protocol['version']}, found {dependency!r}"
        )
    return errors


def expected_package_files() -> set[str]:
    return {
        path.relative_to(PACKAGE_DIR.parent).as_posix()
        for path in PACKAGE_DIR.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def check_wheel(path: Path, version: str) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as wheel:
        names = {name for name in wheel.namelist() if not name.endswith("/")}
        package_files = {name for name in names if name.startswith("kaiwen_agent/")}
        missing = expected_package_files() - package_files
        extra = package_files - expected_package_files()
        if missing:
            errors.append(f"wheel is missing source files: {sorted(missing)[:10]}")
        if extra:
            errors.append(f"wheel contains unexpected files: {sorted(extra)[:10]}")
        for required in ("kaiwen_agent/py.typed", "kaiwen_agent/kits/coding/prompts/base.md"):
            if required not in names:
                errors.append(f"wheel is missing required resource: {required}")
        metadata_name = f"kaiwen_agent-{version}.dist-info/METADATA"
        if metadata_name not in names:
            errors.append(f"wheel metadata does not match version {version}")
        else:
            metadata = wheel.read(metadata_name).decode("utf-8")
            if "Name: kaiwen-agent" not in metadata or f"Version: {version}" not in metadata:
                errors.append("wheel METADATA has an unexpected name or version")
            if "\n\n" not in metadata or len(metadata.split("\n\n", 1)[1].strip()) < 500:
                errors.append("wheel METADATA has no long description")
    return errors


def check_sdist(path: Path, version: str) -> list[str]:
    errors: list[str] = []
    prefix = f"kaiwen_agent-{version}/"
    with tarfile.open(path) as sdist:
        names = [member.name for member in sdist.getmembers() if member.isfile()]
    for name in names:
        if not name.startswith(prefix):
            errors.append(f"sdist member outside {prefix}: {name}")
            continue
        relative = name[len(prefix) :]
        if relative.startswith(FORBIDDEN_SDIST_PREFIXES):
            errors.append(f"sdist contains a path outside the intended contents: {relative}")
    relatives = {name[len(prefix) :] for name in names if name.startswith(prefix)}
    for required in ("PKG-INFO", "pyproject.toml", "README.md", "LICENSE"):
        if required not in relatives:
            errors.append(f"sdist is missing {required}")
    if not any(name.startswith("src/kaiwen_agent/") for name in relatives):
        errors.append("sdist contains no package source")
    return errors


def check_dist(directory: Path) -> list[str]:
    version = str(python_project()["version"])
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return [f"expected exactly one wheel and one sdist in {directory}"]
    errors = check_wheel(wheels[0], version)
    errors += check_sdist(sdists[0], version)
    for artifact in (wheels[0], sdists[0]):
        if f"kaiwen_agent-{version}" not in artifact.name:
            errors.append(f"artifact name does not match version {version}: {artifact.name}")
    return errors


def main(argv: list[str]) -> int:
    if argv[:1] == ["metadata"] and len(argv) == 1:
        errors = check_metadata()
    elif argv[:1] == ["dist"] and len(argv) == 2:
        errors = check_dist(Path(argv[1]))
    else:
        print(__doc__)
        return 2
    for error in errors:
        print(f"ERROR: {error}")
    if not errors:
        print("OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
