from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from kaiwen_agent.integrations import WorkspaceFilesystem
from kaiwen_agent.kits.coding.fingerprint import WorkspaceFingerprintProvider
from kaiwen_agent.kits.coding.verification.pipeline import TrustedEvidencePipeline
from kaiwen_agent.runtime import WorkspaceBinding
from kaiwen_agent.toolkits import CommandCatalog


@dataclass(frozen=True, slots=True)
class AuthorizedCodingWorkspace:
    """One host-authorized workspace and everything that must agree about it.

    The filesystem policy, the fingerprint provider, the command catalog, the sandbox
    runner, the evidence pipeline and (when given) the controller must all refer to this
    workspace. `issues()` lists every inconsistency, so a coding worker that can produce
    execution evidence is refused at configuration instead of producing evidence for the
    wrong tree. It is host configuration, never derived from task input.
    """

    filesystem: WorkspaceFilesystem
    root: str
    fingerprints: WorkspaceFingerprintProvider
    catalog: CommandCatalog
    runner: object
    pipeline: TrustedEvidencePipeline
    # Host-assigned opaque identity of the project (for example its record id). It is not a
    # path, so moving the project to another directory or machine keeps the binding valid;
    # two different projects must be given different identities. It is part of the binding
    # as a keyed-by-scope digest and is required for workers that produce evidence.
    identity: str | None = None

    @property
    def binding(self) -> WorkspaceBinding:
        """The value stored in the durable run record for runs on this workspace."""

        policy = {
            "ignored_names": sorted(self.filesystem.ignored_names),
            "max_read_bytes": self.filesystem.max_read_bytes,
            "max_write_bytes": self.filesystem.max_write_bytes,
            "tenant_id": self.filesystem.tenant_id,
            "user_id": self.filesystem.user_id,
        }
        checksum = sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
        identity_digest: str | None = None
        if self.identity is not None:
            if not self.identity.strip():
                raise ValueError("Workspace identity cannot be blank")
            material = "".join(
                (
                    "kaiwen.workspace.identity.v1",
                    self.filesystem.tenant_id or "",
                    self.filesystem.user_id or "",
                    self.filesystem.workspace_id,
                    self.identity,
                )
            )
            identity_digest = sha256(material.encode()).hexdigest()
        return WorkspaceBinding(
            workspace_id=self.filesystem.workspace_id,
            root=self.root,
            policy_checksum=checksum,
            identity_digest=identity_digest,
        )

    def issues(self, *, controller: Any = None) -> list[str]:
        problems: list[str] = []
        if self.root not in self.filesystem.roots:
            problems.append("root_not_in_workspace")
        builder = getattr(self.fingerprints, "builder", None)
        if (
            builder is None
            or builder.filesystem is not self.filesystem
            or builder.root != self.root
        ):
            problems.append("fingerprint_provider_bound_to_another_workspace")
        if self.catalog.filesystem is not self.filesystem or self.catalog.root != self.root:
            problems.append("command_catalog_bound_to_another_workspace")
        if (
            getattr(self.runner, "filesystem", None) is not self.filesystem
            or getattr(self.runner, "root", None) != self.root
        ):
            problems.append("runner_not_bound_to_this_workspace")
        if not _same_runner(self.catalog.runner, self.runner):
            problems.append("catalog_uses_a_different_runner")
        if not self.pipeline.recorder.trusts(self.runner):
            problems.append("recorder_does_not_trust_the_runner")
        if self.pipeline.recorder not in self.catalog.observers:
            problems.append("recorder_not_attached_to_the_catalog")
        if controller is not None:
            problems.extend(self._controller_issues(controller))
        return problems

    def _controller_issues(self, controller: Any) -> list[str]:
        problems: list[str] = []
        if getattr(controller, "fingerprint_provider", None) is not self.fingerprints:
            problems.append("controller_missing_the_workspace_fingerprint_provider")
        if getattr(controller, "workspace_binding", None) != self.binding:
            problems.append("controller_not_bound_to_this_workspace")
        gate = getattr(controller, "completion_gate", None)
        if gate is None or gate.artifacts is not self.pipeline.artifacts:
            problems.append("gate_uses_different_artifacts")
        else:
            for verifier_id in ("coding.tests", "coding.acceptance"):
                verifier = gate.verifiers.get(verifier_id)
                if getattr(verifier, "signer", None) is not self.pipeline.signer:
                    problems.append(f"verifier_not_using_the_pipeline_signer:{verifier_id}")
        return problems


def _same_runner(left: object, right: object) -> bool:
    if left is right:
        return True
    domain = getattr(left, "provenance_domain", None)
    return domain is not None and domain is getattr(right, "provenance_domain", None)
