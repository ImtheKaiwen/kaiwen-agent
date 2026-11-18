"""M6.4-C R5-1: a paid run is pinned to one authorized commit and a clean tree.

Real throwaway Git repositories (no network, no model) exercise the check: the authorization must
name the full commit, the commit must be the checked-out HEAD of the repository this script belongs
to, the working tree must hold no change that could alter the executed code (ignored local
configuration and report files are fine), the package that would run must be this checkout's, and
anything that cannot be established refuses. Every refusal happens before any request.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import live_team_validation as lt
from scripts import live_validation as lv

PRICES = ["--price-input-per-mtok=0.75", "--price-output-per-mtok=4.5"]


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    git(tmp_path, "init", "-q", str(root))
    (root / ".gitignore").write_text(".env\n.kaiwen-live/\n")
    (root / "src" / "module.py").write_text("VALUE = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD")


def check(root: Path, authorized: str | None, **options: Any) -> str | None:
    options.setdefault("package_file", root / "src" / "module.py")
    return lt.verify_authorized_commit(authorized, root=root, **options)


def test_the_exact_head_of_a_clean_checkout_is_accepted(repo: Path) -> None:
    assert check(repo, head(repo)) is None


def test_ignored_local_configuration_and_reports_do_not_block_a_run(repo: Path) -> None:
    (repo / ".env").write_text("OPENAI_API_KEY=not-a-real-key\n")
    (repo / ".kaiwen-live").mkdir()
    (repo / ".kaiwen-live" / "report.json").write_text("{}")
    assert check(repo, head(repo)) is None


@pytest.mark.parametrize("authorized", [None, "", "  ", "abc123", "HEAD", "g" * 40, "A" * 40])
def test_the_authorization_must_name_a_full_commit(repo: Path, authorized: str | None) -> None:
    assert "40-character" in (check(repo, authorized) or "")


def test_a_different_commit_is_refused(repo: Path) -> None:
    previous = head(repo)
    (repo / "src" / "module.py").write_text("VALUE = 2\n")
    git(repo, "commit", "-aqm", "second")
    assert head(repo) != previous
    assert "not the authorized commit" in (check(repo, previous) or "")
    assert check(repo, head(repo)) is None


def test_a_modified_tracked_file_is_refused(repo: Path) -> None:
    (repo / "src" / "module.py").write_text("VALUE = 99\n")
    assert "uncommitted" in (check(repo, head(repo)) or "")


def test_an_untracked_file_that_is_not_ignored_is_refused(repo: Path) -> None:
    (repo / "src" / "extra.py").write_text("import os\n")
    assert "uncommitted" in (check(repo, head(repo)) or "")


def test_a_staged_change_is_refused(repo: Path) -> None:
    (repo / "src" / "module.py").write_text("VALUE = 5\n")
    git(repo, "add", "-A")
    assert "uncommitted" in (check(repo, head(repo)) or "")


def test_unavailable_git_state_fails_closed(repo: Path, tmp_path: Path) -> None:
    assert "cannot be established" in (check(repo, head(repo), git="no-such-git-binary") or "")
    plain = tmp_path / "not-a-repository"
    plain.mkdir()
    assert "cannot be established" in (check(plain, "a" * 40) or "")


def test_a_subdirectory_of_a_repository_is_not_the_repository(repo: Path) -> None:
    assert check(repo / "src", head(repo)) is not None


def test_the_package_that_would_run_must_belong_to_this_checkout(
    repo: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "another-checkout" / "src" / "module.py"
    assert "this checkout" in (check(repo, head(repo), package_file=elsewhere) or "")


def test_git_environment_overrides_cannot_redirect_the_check(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(tmp_path, "init", "-q", str(other))
    (other / "f").write_text("x")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "other")
    mine, theirs = head(repo), head(other)  # read before the environment is poisoned
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    # The check must read THIS repository, whatever the process environment says.
    assert check(repo, mine) is None
    assert check(repo, theirs) is not None


# ------------------------------------------------------------------------ the command line


def live_arguments(*extra: str) -> list[str]:
    return [
        "run",
        "--live",
        *PRICES,
        "--authorize-model",
        "gpt-5.4-mini",
        "--authorize-max-usd",
        "0.15",
        "--authorize-scenarios",
        "LIVE_ACCEPTED",
        *extra,
    ]


@pytest.fixture
def no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("nothing may start: the commit authorization was not satisfied")

    monkeypatch.setattr(lt, "run_team", forbidden)
    monkeypatch.setattr(lv, "_real_client", forbidden)


def test_a_live_run_without_authorize_head_is_refused_before_anything_starts(
    no_request: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lt.main(live_arguments("--budget-usd", "0.15", "--max-requests", "12")) == 2
    assert "authorize-head" in capsys.readouterr().err


def test_a_live_run_with_a_refused_commit_is_refused_before_anything_starts(
    no_request: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(lt, "verify_authorized_commit", lambda *a, **k: "dirty tree")
    code = lt.main(live_arguments("--authorize-head", "a" * 40, "--budget-usd", "0.15"))
    assert code == 2 and "dirty tree" in capsys.readouterr().err


def test_the_commit_is_verified_for_every_live_run_and_not_for_a_dry_run_or_a_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[Any] = []

    def refuse(authorized: Any, **options: Any) -> str:
        calls.append(authorized)
        return "refused for this test"

    monkeypatch.setattr(lt, "verify_authorized_commit", refuse)
    monkeypatch.setattr(lv, "_real_client", lambda *a, **k: pytest.fail("a client was created"))
    env = ["--env-file", str(tmp_path / "none.env")]
    assert lt.main(["run", *PRICES, *env]) == 0  # a dry run needs no authorization
    assert lt.main(["plan", *PRICES]) in (0, 1)  # neither does the offline plan
    assert calls == []
    assert lt.main(live_arguments("--authorize-head", "b" * 40, *env)) == 2
    assert calls == ["b" * 40]
