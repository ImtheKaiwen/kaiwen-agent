"""Evidence recorded from real sandbox executions and the signature that authenticates it.

Evidence is written only by `CommandEvidenceRecorder`, a `CommandExecutionObserver` that
receives the runner's own `SandboxResult`. The pass/fail status is derived from the exit
code and outcome flags; a model never supplies it. Each artifact carries an HMAC computed
with a host-held key, so a verifier configured with the same signer can reject any
artifact that was not produced by the trusted execution path.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kaiwen_agent.artifacts import ArtifactContext, ArtifactCreate, ArtifactManager, ArtifactType
from kaiwen_agent.audit import SecretRedactor
from kaiwen_agent.toolkits.commands import CommandExecution

EVIDENCE_ORIGIN = "trusted_sandbox"
EVIDENCE_SCHEMA_VERSION = 1
_MAC_LABEL = "evidence_mac"
_STREAM_TEXT_BYTES = 20_000


@dataclass(frozen=True, slots=True)
class EvidenceSigner:
    """Host-held key that authenticates evidence artifacts."""

    key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.key) < 16:
            raise ValueError("Evidence signing key must contain at least 16 bytes")

    def sign(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        run_id: str | None,
        content: bytes,
        labels: Mapping[str, str],
    ) -> str:
        return hmac.new(
            self.key, _canonical(tenant_id, owner_user_id, run_id, content, labels), sha256
        ).hexdigest()

    def verify(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        run_id: str | None,
        content: bytes,
        labels: Mapping[str, str],
    ) -> bool:
        supplied = labels.get(_MAC_LABEL)
        if supplied is None:
            return False
        expected = self.sign(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            run_id=run_id,
            content=content,
            labels=labels,
        )
        return hmac.compare_digest(expected, supplied)


def _canonical(
    tenant_id: str,
    owner_user_id: str,
    run_id: str | None,
    content: bytes,
    labels: Mapping[str, str],
) -> bytes:
    material = {
        "tenant": tenant_id,
        "owner": owner_user_id,
        "run": run_id,
        "content_sha256": sha256(content).hexdigest(),
        "labels": {key: value for key, value in sorted(labels.items()) if key != _MAC_LABEL},
    }
    return json.dumps(material, sort_keys=True, separators=(",", ":")).encode()


class EvidenceBinding(BaseModel):
    """Host mapping from a command preset to the requirement its result can evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(min_length=1, max_length=200)
    requirement_id: str = Field(min_length=1, max_length=200)
    evidence_type: str = Field(min_length=1, max_length=100)


class CommandEvidenceRecorder:
    """Records trusted execution results as signed, owner- and run-scoped artifacts."""

    def __init__(
        self,
        *,
        artifacts: ArtifactManager,
        signer: EvidenceSigner,
        bindings: Iterable[EvidenceBinding],
        trusted_runners: Iterable[object],
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._trusted = tuple(trusted_runners)
        if not self._trusted:
            raise ValueError("At least one trusted runner must be named")
        self.artifacts = artifacts
        self.signer = signer
        self.redactor = redactor or SecretRedactor()
        self._bindings: dict[str, list[EvidenceBinding]] = {}
        for binding in bindings:
            self._bindings.setdefault(binding.command_id, []).append(binding)

    def trusts(self, runner: object) -> bool:
        """Whether the host named this runner (or its provenance domain) as trusted.

        Trust comes from the runner object the host configured on the catalog, never from
        fields of a `SandboxResult`, which any runner (including a fake) can populate.
        """

        domain = getattr(runner, "provenance_domain", None)
        for trusted in self._trusted:
            if runner is trusted:
                return True
            if domain is not None and getattr(trusted, "provenance_domain", None) is domain:
                return True
        return False

    async def observe(self, execution: CommandExecution) -> tuple[str, ...]:
        bindings = self._bindings.get(execution.preset.id, [])
        if not bindings or not self.trusts(execution.runner):
            return ()
        result = execution.result
        context = execution.context
        run_id = context.metadata.get("runtime_run_id")
        if (
            not isinstance(run_id, str)
            or not run_id
            or context.tenant_id is None
            or context.user_id is None
            or result.snapshot_fingerprint is None
        ):
            # Without a trusted run, owner, and executed snapshot nothing may be claimed.
            return ()
        artifact_context = ArtifactContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            trace_id=context.trace_id or f"trace_{run_id}",
            run_id=run_id,
        )
        outcome = _outcome(
            result.exit_code,
            result.timed_out,
            result.cancelled,
            result.snapshot_unchanged,
            result.cleanup_failed,
        )
        references: list[str] = []
        for binding in bindings:
            payload = {
                "schema_version": EVIDENCE_SCHEMA_VERSION,
                "origin": EVIDENCE_ORIGIN,
                "run_id": run_id,
                "workspace_id": context.metadata.get("workspace_id"),
                "requirement_id": binding.requirement_id,
                "evidence_type": binding.evidence_type,
                "command_id": execution.preset.id,
                "category": execution.preset.category,
                "parameters": dict(execution.parameters),
                "snapshot_fingerprint": result.snapshot_fingerprint,
                "image": {"reference": result.image_ref, "id": result.image_id},
                "exit_code": result.exit_code,
                "outcome": outcome,
                "timed_out": result.timed_out,
                "cancelled": result.cancelled,
                "duration_seconds": result.duration_seconds,
                "output_truncated": result.output_truncated,
                "snapshot_unchanged": result.snapshot_unchanged,
                "cleanup_failed": result.cleanup_failed,
                "modified_paths": list(result.modified_paths),
                "stdout": self._stream(result.stdout),
                "stderr": self._stream(result.stderr),
            }
            content = json.dumps(payload, sort_keys=True).encode()
            labels = {
                "evidence_origin": EVIDENCE_ORIGIN,
                "requirement_id": binding.requirement_id,
                "evidence_type": binding.evidence_type,
                "input_fingerprint": result.snapshot_fingerprint,
                "command_id": execution.preset.id,
                "outcome": outcome,
            }
            labels[_MAC_LABEL] = self.signer.sign(
                tenant_id=artifact_context.tenant_id,
                owner_user_id=artifact_context.user_id,
                run_id=run_id,
                content=content,
                labels=labels,
            )
            metadata = await self.artifacts.create(
                artifact_context,
                ArtifactCreate(
                    type=ArtifactType.REPORT,
                    mime_type="application/json",
                    content=content,
                    labels=labels,
                ),
            )
            references.append(metadata.id)
        return tuple(references)

    def _stream(self, text: str) -> dict[str, Any]:
        encoded = text.encode()
        redacted = self.redactor.redact(text)
        return {
            "sha256": sha256(encoded).hexdigest(),
            "bytes": len(encoded),
            "text": redacted.encode()[:_STREAM_TEXT_BYTES].decode("utf-8", errors="ignore"),
            "text_truncated": len(redacted.encode()) > _STREAM_TEXT_BYTES,
        }


def _outcome(
    exit_code: int,
    timed_out: bool,
    cancelled: bool,
    snapshot_unchanged: bool | None,
    cleanup_failed: bool | None,
) -> str:
    """The command exit code alone is a command result; verified completion needs more."""

    if cancelled:
        return "cancelled"
    if timed_out:
        return "timed_out"
    if exit_code != 0:
        return "failed"
    if snapshot_unchanged is False:
        return "snapshot_modified"
    if cleanup_failed is True:
        return "cleanup_failed"
    if snapshot_unchanged is True and cleanup_failed is False:
        return "passed"
    return "unverified"
