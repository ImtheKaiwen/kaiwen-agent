# Quick start: the coding-team example

Status: first-beta instructions for a source checkout. This runs a deterministic, credential-free coding
team (Lead, Coder, Tester, Reviewer) through the real runtime and prints host-controlled outcomes. It is a
preview for evaluation, not a production release. Known limits are listed at the end and in
[`coding-team-beta.md`](coding-team-beta.md).

## What you need

| Requirement | Needed for | Notes |
| --- | --- | --- |
| Python 3.11 or newer | everything | 3.10 is refused by `pip` (see Common errors) |
| `git` | getting the source | or download a source archive from the GitHub release |
| Network access to PyPI | installing `pydantic` | the only runtime dependency of the core package |
| Docker with a Linux engine | the optional Docker scenarios only | not needed for the offline example |
| An API key | nothing here | the example never calls a model provider |

No configuration file or environment variable is required. Optional variables (Docker scenarios only):

- `KAIWEN_DOCKER_IMAGE`: another digest-pinned image reference (`name@sha256:...`). Default
  `python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7`.
- `KAIWEN_REQUIRE_DOCKER=1`: make a missing engine or image a failure (exit code 1) instead of a skip.

## Install

The example lives in the repository, not in the wheel, so use a source checkout and install the package
into a virtual environment.

PowerShell:

```powershell
git clone https://github.com/ImtheKaiwen/kaiwen-agent.git
cd kaiwen-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

Bash:

```bash
git clone https://github.com/ImtheKaiwen/kaiwen-agent.git
cd kaiwen-agent
python3 -m venv .venv
.venv/bin/python -m pip install .
```

To install the published beta instead of the checkout's version, use `python -m pip install "kaiwen-agent==0.8.0b1"` and check out the tag `v0.8.0b1` for the example. Use `pip install -e .` instead if you intend to edit the package. A release also attaches a wheel and an
sdist; `pip install kaiwen_agent-<version>-py3-none-any.whl` installs the package the same way, and the
example is then run from a source checkout (or its `examples/coding-team-acceptance/main.py` copied out).

## Run the offline example

From the repository root, call the interpreter of the environment you installed into (this avoids having
to activate it):

```powershell
.\.venv\Scripts\python.exe examples\coding-team-acceptance\main.py
```

```bash
.venv/bin/python examples/coding-team-acceptance/main.py
```

Expected output, ten lines and a summary, exit code 0:

```text
[ok ] accepted                             -> completed run=completed reason=None acceptance=accepted:accepted
[ok ] same_cycle                           -> completed run=completed reason=None acceptance=accepted:accepted
[ok ] human_approved                       -> completed run=completed reason=None acceptance=accepted:accepted
[ok ] changes_requested                    -> blocked   run=blocked   reason=team_changes_required:reviewer_requested_changes acceptance=changes_required:reviewer_requested_changes
[ok ] failing_tests                        -> blocked   run=blocked   reason=quality_blocked acceptance=not_evaluated
[ok ] unrelated_evidence                   -> blocked   run=blocked   reason=team_changes_required:evidence_not_gate_verified acceptance=changes_required:evidence_not_gate_verified
[ok ] no_reviewer                          -> blocked   run=blocked   reason=team_blocked:review_missing acceptance=blocked:review_missing
[ok ] approval_denied                      -> blocked   run=blocked   reason=team_blocked:human_denied acceptance=blocked:human_denied
[ok ] approval_for_other_delivery          -> blocked   run=blocked   reason=team_blocked:approval_mismatch acceptance=blocked:approval_mismatch
[ok ] workspace_changed_during_approval    -> blocked   run=blocked   reason=workspace_changed_after_acceptance acceptance=accepted:accepted
Every scenario ended as documented; only the accepted deliveries completed.
```

How to read it: `[ok ]` means the scenario ended as documented. Only the first three deliveries complete;
every other row is blocked by the host (the completion gate or the team acceptance), which is the correct
result for that scenario. The outcome comes from the platform's records, not from anything a model says.
To run one scenario: `... main.py accepted`. An unknown name exits with code 2.

## Run the Docker scenarios (optional)

These run the Tester's `testing.run` command inside a digest-pinned, network-less, read-only container.
They need a reachable Linux Docker engine and the image present locally; the runner never pulls and has no
local fallback.

```powershell
docker pull python@sha256:e064671ca452259bab50e8e2c7bd896e39e9a343bb06ca25234adf6fe18d0fc7
.\.venv\Scripts\python.exe examples\coding-team-acceptance\main.py --docker
```

Expected: five `[ok ]` lines (`docker_accepted`, `docker_failing_tests`, `docker_unrelated_evidence`,
`docker_unregistered_command`, `docker_unregistered_tool`) and exit code 0. Without an engine or the image the
command prints `Docker sandbox unavailable (...); nothing was executed.` and exits 0 (1 with
`KAIWEN_REQUIRE_DOCKER=1`); it never falls back to running commands on your machine.

## Common errors

| Message | Cause and fix |
| --- | --- |
| `ERROR: Package 'kaiwen-agent' requires a different Python: 3.10.x not in '>=3.11'` | Use Python 3.11 or newer to create the environment. |
| `ModuleNotFoundError: No module named 'kaiwen_agent'` | The interpreter you ran is not the one the package was installed into. Run the example with the virtual environment's own `python`. |
| `Docker sandbox unavailable (The sandbox image is not available locally); nothing was executed.` | Pull the pinned image (above), or start the Docker engine. The offline example does not need Docker. |
| `Unknown scenario. Available: ...` (exit 2) | The scenario name is misspelled; names are listed in the message. |
| A `[BAD]` row and a non-zero exit | A scenario did not end as documented. This is a defect; report it with the full output. |

## Verified and unverified environments

Verified by running these commands (2026-11-18): Windows 11, installing the wheel (Python 3.13 and 3.11), the sdist
and `pip install .` from a fresh clone of `main` (Python 3.13) into clean virtual environments, the offline example,
and the Docker scenarios against Docker Desktop (Linux engine 28.3.0). Continuous integration additionally
runs the full test suite on Linux (Python 3.11, 3.12, 3.13) and Windows (3.11, 3.13), and the Docker
scenarios on Linux with a real engine; CI installs the package in editable mode, not from a wheel.

Not verified by this quick start: macOS; a Linux workstation install by these exact commands; Python 3.12
on Windows; Docker engines other than the two named above; Windows containers (not supported); Podman.

## Limits you should know

- This is a deterministic composition: the Lead, Coder, Tester and Reviewer are scripted in the default
  mode. It shows that the platform's host controls decide the outcome; it does not show model quality.
- One real-provider run of the accepted scenario was validated (maintainer-run, one model, one small
  project, 2026-11-18, at an earlier commit); later corrections were verified offline and by CI only. Do
  not run `scripts/live_team_validation.py --live`: it spends money and needs a separate authorization.
- No author-independent review has taken place. Not production ready.
