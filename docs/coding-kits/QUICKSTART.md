# Coding Kits quickstart

Coding Kits are opt-in. Importing `kaiwen_agent` alone does not load prompts, tools,
providers, files, processes or network clients.

For the additive low-boilerplate facade, see [Simple Coding API](SIMPLE_API.md).

## Single agent

Create the normal platform registries and an `AgentFactory`, register a host-owned
provider factory, then compose the kit:

```python
from kaiwen_agent.kits.coding import CodingKitConfig, CodingKitFactory

coding = CodingKitFactory(agent_factory).create(
    CodingKitConfig(provider_ref="openai:configured")
)
result = await coding.chat("Bu projeyi analiz et", identity=trusted_identity)
```

Provider keys stay in the host. Use the same shape for Gemini or Ollama; only the
registered provider reference changes. Tool workflows require truthful registered
model capabilities and `check_provider_compatibility` before execution.

## Existing projects

Bind `WorkspaceFilesystem` to a trusted root, then run `ProjectDiscovery`. Discovery
is read-only, bounded, excludes common secrets/vendor data, records manifest hashes
and returns unknown rather than guessing unsupported facts.

## Safe actions and testing

Use `workspace.apply_patch` with an exact preimage SHA-256. Register exact command
presets in `CommandCatalog` and inject a host sandbox implementing `SandboxRunner`.
The library deliberately has no unrestricted shell or fallback subprocess runner. For a
real isolated runner on a trusted Linux Docker engine, use `DockerSandboxRunner` over an
execution snapshot with `CommandEvidenceRecorder`; see
[Permissions and sandbox boundary](PERMISSIONS_AND_SANDBOX.md) and
`examples/coding-docker-evidence`.

## Teams

`CodingTeamFactory` creates exact role runtimes and a `TeamDefinition`. Delegate with
the existing `TeamRuntime` and `JobManager`. Optional architect, designer and DevOps
roles activate only from trusted project facts. Writes default to serialized.

## Offline/fake path

Tests and examples may register `FakeModelProvider`. A fake proves control flow, not
model quality or live provider support. It must never be reported as a live smoke.
