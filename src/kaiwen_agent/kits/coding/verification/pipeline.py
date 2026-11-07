from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from kaiwen_agent.artifacts import ArtifactManager
from kaiwen_agent.kits.coding.verification.artifacts import (
    JsonArtifactVerifier,
    install_coding_verifiers,
)
from kaiwen_agent.kits.coding.verification.evidence import (
    CommandEvidenceRecorder,
    EvidenceBinding,
    EvidenceSigner,
)
from kaiwen_agent.quality import VerifierRegistry


@dataclass(frozen=True, slots=True)
class TrustedEvidencePipeline:
    """One coherent evidence identity: the recorder and the verifiers share these objects.

    It only wires existing components: the same `ArtifactManager` stores what the recorder
    writes and the verifiers read, and the same `EvidenceSigner` signs and checks it. A
    signer is mandatory, so a pipeline built here can never fall back to accepting
    unsigned JSON for command requirements. Keep the signer out of agents and tool results.
    """

    artifacts: ArtifactManager
    signer: EvidenceSigner
    recorder: CommandEvidenceRecorder

    @classmethod
    def build(
        cls,
        *,
        artifacts: ArtifactManager,
        signer: EvidenceSigner | None,
        bindings: Iterable[EvidenceBinding],
        trusted_runners: Iterable[object],
    ) -> TrustedEvidencePipeline:
        if not isinstance(signer, EvidenceSigner):
            raise ValueError("Trusted evidence requires an EvidenceSigner")
        recorder = CommandEvidenceRecorder(
            artifacts=artifacts,
            signer=signer,
            bindings=bindings,
            trusted_runners=trusted_runners,
        )
        return cls(artifacts=artifacts, signer=signer, recorder=recorder)

    def install_verifiers(self, registry: VerifierRegistry) -> tuple[JsonArtifactVerifier, ...]:
        return install_coding_verifiers(registry, self.artifacts, signer=self.signer)
