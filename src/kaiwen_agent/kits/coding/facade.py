from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from kaiwen_agent.agents import AgentDefinitionRegistry
from kaiwen_agent.capabilities import CapabilityRegistry
from kaiwen_agent.kits.coding.factory import (
    CodingAgent,
    CodingKitConfig,
    CodingKitFactory,
    CodingPreflightIssue,
    CodingPreflightResult,
)
from kaiwen_agent.kits.coding.handles import CodingRunHandle
from kaiwen_agent.kits.coding.profiles import CodingMode, CodingProfile
from kaiwen_agent.kits.coding.prompting import (
    CodingConversationPreferences,
    CodingUserLanguage,
)
from kaiwen_agent.kits.coding.providers import (
    CodingProviderPreset,
    ProviderRequirement,
    check_provider_compatibility,
)
from kaiwen_agent.kits.coding.simple_config import (
    CodingExecutionBinding,
    CodingPreflightMode,
    CodingSimpleAPIError,
)
from kaiwen_agent.models import (
    DataLocality,
    GeminiGenerateContentProvider,
    ModelCapabilities,
    ModelCapabilitiesRegistry,
    ModelProvider,
    OllamaChatProvider,
    OpenAIResponsesProvider,
    ProviderRegistry,
)
from kaiwen_agent.prompts import PromptRegistry
from kaiwen_agent.runtime import AgentFactory, RuntimeIdentity, RuntimeRunStatus
from kaiwen_agent.standards import StandardsRegistry
from kaiwen_agent.tools import ToolRegistry

ProviderFactory = Callable[[str], ModelProvider]


class SimpleCodingAgent:
    """Identity-bound convenience wrapper; the existing CodingAgent is unchanged."""

    def __init__(self, kit: CodingKit) -> None:
        self._kit = kit

    async def chat(self, text: str) -> Any:
        agent = self._kit._configured_agent()
        return await agent.chat(text, identity=self._kit.identity)

    async def action(self, text: str, *, tools: Sequence[str]) -> Any:
        selected = tuple(tools)
        report = self._kit.preflight(mode=CodingPreflightMode.ACTION, tool_names=selected)
        self._kit._raise_preflight(report)
        agent = self._kit._configured_agent()
        return await agent.action(text, identity=self._kit.identity, tool_names=selected)

    async def start(self, objective: str) -> CodingRunHandle:
        return await self._kit._start(objective, team=False)


class SimpleCodingTeam:
    """Optional team entry point backed by an explicitly supplied TeamRuntime."""

    def __init__(self, kit: CodingKit) -> None:
        self._kit = kit

    async def start(self, objective: str) -> CodingRunHandle:
        return await self._kit._start(objective, team=True)


class CodingKit:
    """Thin opt-in Developer Experience facade over existing Coding Kit/runtime APIs."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        identity: RuntimeIdentity,
        agent_factory: AgentFactory,
        provider_ref: str,
        profile: CodingProfile = CodingProfile.BALANCED,
        mode: CodingMode = CodingMode.SUPERVISED,
        execution: CodingExecutionBinding | None = None,
        model_capabilities: ModelCapabilitiesRegistry | None = None,
        provider_preset: CodingProviderPreset | None = None,
        data_locality: DataLocality = DataLocality.CLOUD_ALLOWED,
        owns_resources: bool = False,
    ) -> None:
        self.workspace = Path(workspace).expanduser().absolute()
        self.workspace_ref = _workspace_ref(self.workspace)
        self.identity = identity
        self.agent_factory = agent_factory
        self.provider_ref = provider_ref
        self.profile = profile
        self.mode = mode
        self.execution = execution
        self.model_capabilities = model_capabilities
        self.provider_preset = provider_preset
        self.data_locality = data_locality
        self.owns_resources = owns_resources
        self._agent: CodingAgent | None = None
        self._closed = False

    @classmethod
    def local(
        cls,
        *,
        workspace: str | Path,
        provider: str,
        model: str,
        profile: CodingProfile | str = CodingProfile.BALANCED,
        mode: CodingMode | str = CodingMode.SUPERVISED,
        execution: CodingExecutionBinding | None = None,
        provider_factory: ProviderFactory | None = None,
        model_capabilities: ModelCapabilities | None = None,
        user_language: CodingUserLanguage | str = CodingUserLanguage.ENGLISH,
        data_locality: DataLocality | str = DataLocality.CLOUD_ALLOWED,
    ) -> CodingKit:
        """Create an explicitly local single-user composer without performing I/O."""

        if not provider.strip() or not model.strip():
            raise ValueError("provider and model must not be empty")
        workspace_path = Path(workspace).expanduser().absolute()
        workspace_ref = _workspace_ref(workspace_path)
        provider_ref = f"coding.local.{provider}.{_short_hash(model)}"
        providers = ProviderRegistry()
        providers.register(
            provider_ref,
            provider_factory or _lazy_provider_factory(provider=provider, model=model),
        )
        capabilities = ModelCapabilitiesRegistry()
        preset: CodingProviderPreset | None = None
        if model_capabilities is not None:
            if (model_capabilities.provider, model_capabilities.model) != (provider, model):
                raise ValueError("model_capabilities must match provider and model")
            capabilities.register(model_capabilities)
            preset = CodingProviderPreset(
                id=provider_ref,
                provider_ref=provider_ref,
                provider=provider,
                model=model,
                prompt_budget_tokens=min(8_192, max(1, model_capabilities.context_window - 1)),
            )
        preferences = CodingConversationPreferences(user_language=CodingUserLanguage(user_language))
        identity = RuntimeIdentity(
            session_id=f"local_{workspace_ref}",
            tenant_id="local",
            user_id="local",
            conversation_id=f"conversation_{workspace_ref}",
            trace_id=f"trace_{uuid4().hex}",
            environment="local",
            permissions=execution.permissions if execution is not None else frozenset(),
            metadata={
                "workspace_id": workspace_ref,
                "local_single_user": True,
                "conversation_preferences": preferences.model_dump(mode="json"),
            },
        )
        factory = AgentFactory(
            agents=AgentDefinitionRegistry(),
            prompts=PromptRegistry(),
            capabilities=CapabilityRegistry(),
            providers=providers,
            tools=execution.tools if execution is not None else ToolRegistry(),
            standards=(
                execution.standards_registry if execution is not None else StandardsRegistry()
            ),
            event_sink=execution.event_sink if execution is not None else None,
            run_store=execution.run_store if execution is not None else None,
            tool_executor=execution.tool_executor if execution is not None else None,
        )
        return cls(
            workspace=workspace_path,
            identity=identity,
            agent_factory=factory,
            provider_ref=provider_ref,
            profile=CodingProfile(profile),
            mode=CodingMode(mode),
            execution=execution,
            model_capabilities=capabilities,
            provider_preset=preset,
            data_locality=DataLocality(data_locality),
            owns_resources=True,
        )

    @classmethod
    def for_host(
        cls,
        *,
        identity: RuntimeIdentity,
        workspace: str | Path,
        agent_factory: AgentFactory,
        provider_ref: str,
        profile: CodingProfile | str = CodingProfile.BALANCED,
        mode: CodingMode | str = CodingMode.SUPERVISED,
        execution: CodingExecutionBinding | None = None,
        model_capabilities: ModelCapabilitiesRegistry | None = None,
        provider_preset: CodingProviderPreset | None = None,
        data_locality: DataLocality | str = DataLocality.CLOUD_ALLOWED,
    ) -> CodingKit:
        if identity.tenant_id is None or identity.user_id is None:
            raise ValueError("Hosted CodingKit requires trusted tenant_id and user_id")
        return cls(
            workspace=workspace,
            identity=identity,
            agent_factory=agent_factory,
            provider_ref=provider_ref,
            profile=CodingProfile(profile),
            mode=CodingMode(mode),
            execution=execution,
            model_capabilities=model_capabilities,
            provider_preset=provider_preset,
            data_locality=DataLocality(data_locality),
        )

    async def __aenter__(self) -> CodingKit:
        self._require_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def agent(self) -> SimpleCodingAgent:
        self._require_open()
        return SimpleCodingAgent(self)

    def team(self) -> SimpleCodingTeam:
        self._require_open()
        return SimpleCodingTeam(self)

    def preflight(
        self,
        *,
        mode: CodingPreflightMode | str = CodingPreflightMode.CHAT,
        team: bool = False,
        tool_names: Sequence[str] = (),
    ) -> CodingPreflightResult:
        selected_mode = CodingPreflightMode(mode)
        issues: list[CodingPreflightIssue] = []
        tools = frozenset(tool_names)
        if len(tuple(tool_names)) != len(tools):
            issues.append(
                CodingPreflightIssue(
                    code="tools_not_allowed", message="Tool names must not contain duplicates."
                )
            )
        config = self._config()
        issues.extend(CodingKitFactory(self.agent_factory).validate_config(config).issues)
        execution = self.execution
        if selected_mode != CodingPreflightMode.CHAT:
            if execution is None:
                issues.append(
                    CodingPreflightIssue(
                        code="durable_store_missing"
                        if selected_mode in {CodingPreflightMode.WORKFLOW, CodingPreflightMode.TEAM}
                        else "tools_not_allowed",
                        message=(
                            "Configure trusted durable workflow execution."
                            if selected_mode
                            in {CodingPreflightMode.WORKFLOW, CodingPreflightMode.TEAM}
                            else "Configure trusted action tools before using action mode."
                        ),
                    )
                )
            else:
                if selected_mode == CodingPreflightMode.ACTION:
                    for name in sorted(tools - execution.action_tools):
                        issues.append(
                            CodingPreflightIssue(
                                code="tools_not_allowed",
                                message=f"Tool is outside the trusted action allowlist: {name}",
                            )
                        )
                    if not tools:
                        issues.append(
                            CodingPreflightIssue(
                                code="tools_not_allowed",
                                message=(
                                    "Action mode requires an explicit non-empty tool allowlist."
                                ),
                            )
                        )
                    issues.extend(self._provider_preflight(requires_structured_output=False))
                    for name in sorted(tools & execution.action_tools):
                        definition = execution.tools.get(name)
                        if (
                            definition.requires_approval
                            and execution.tool_executor.approval_gate is None
                        ):
                            issues.append(
                                CodingPreflightIssue(
                                    code="approval_required",
                                    message=(f"Tool '{name}' requires a configured approval gate."),
                                )
                            )
                        if (
                            not definition.idempotent or definition.side_effect == "destructive"
                        ) and execution.tool_executor.action_intent_store is None:
                            issues.append(
                                CodingPreflightIssue(
                                    code="durable_store_missing",
                                    message=(
                                        f"Tool '{name}' requires durable action-intent receipts."
                                    ),
                                )
                            )
                if execution.requires_sandbox and execution.sandbox is None:
                    issues.append(
                        CodingPreflightIssue(
                            code="sandbox_missing",
                            message="Configure a real host sandbox for command/test execution.",
                        )
                    )
                if selected_mode in {CodingPreflightMode.WORKFLOW, CodingPreflightMode.TEAM}:
                    issues.extend(self._workflow_preflight())
                if (team or selected_mode == CodingPreflightMode.TEAM) and execution.team is None:
                    issues.append(
                        CodingPreflightIssue(
                            code="team_not_configured",
                            message=(
                                "Configure CodingTeamFactory, TeamRuntime, and a team launcher."
                            ),
                        )
                    )
                elif team or selected_mode == CodingPreflightMode.TEAM:
                    issues.extend(self._team_preflight())
        if (team or selected_mode == CodingPreflightMode.TEAM) and (
            execution is None or execution.team is None
        ):
            issues.append(
                CodingPreflightIssue(
                    code="team_not_configured",
                    message="Configure CodingTeamFactory, TeamRuntime, and a team launcher.",
                )
            )
        if selected_mode != CodingPreflightMode.CHAT and not self.workspace.is_dir():
            issues.append(
                CodingPreflightIssue(
                    code="workspace_not_authorized",
                    message="The configured workspace must exist and be a directory.",
                )
            )
        return CodingPreflightResult(issues=tuple(_unique_issues(issues)))

    async def resume(self, run_id: str) -> CodingRunHandle:
        self._require_open()
        execution = self.execution
        if execution is None:
            raise CodingSimpleAPIError(
                "durable_store_missing", "Resume requires trusted durable workflow execution."
            )
        run = await execution.runs.get(run_id=run_id, identity=self.identity)
        if run is None:
            raise KeyError("Coding run was not found in the trusted owner scope")
        team_binding = execution.team
        team_lead = team_binding.configured.runtimes.get("lead") if team_binding else None
        if team_lead is not None and run.version_pins == team_lead.pins:
            assert team_binding is not None
            mode = "team"
            reference = (
                f"{team_binding.configured.definition.id}@"
                f"{team_binding.configured.definition.version}"
            )
        else:
            configured = self._configured_agent().configured
            mode = "workflow"
            reference = f"{configured.definition.id}@{configured.definition.version}"
        if mode == "workflow" and run.version_pins != configured.pins:
            raise CodingSimpleAPIError(
                "resume_configuration_mismatch",
                "The current provider, prompt, tools, standards, or workspace pins differ.",
            )
        task: asyncio.Task[Any] | None = None
        if run.status not in {
            RuntimeRunStatus.COMPLETED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLED,
        }:
            if execution.control is None:
                raise CodingSimpleAPIError(
                    "resume_unsupported",
                    "Configure a durable host resumer that reconciles scheduler state.",
                )
            task = asyncio.create_task(execution.control.resume(run=run, identity=self.identity))
        return CodingRunHandle(
            run_id=run.id,
            objective=None,
            mode=mode,
            agent_or_team_ref=reference,
            workspace_ref=self.workspace_ref,
            identity=self.identity,
            execution=execution,
            task=task,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self.owns_resources or self._agent is None:
            return
        model = self._agent.configured.runtime.model
        closer = getattr(model, "aclose", None) or getattr(model, "close", None)
        if callable(closer):
            result = closer()
            if inspect.isawaitable(result):
                await result

    def _config(self) -> CodingKitConfig:
        execution = self.execution
        return CodingKitConfig(
            id=f"coding.solo.{self.workspace_ref}",
            provider_ref=self.provider_ref,
            tool_refs=execution.action_tools if execution is not None else frozenset(),
            permissions=execution.permissions if execution is not None else frozenset(),
            profile=self.profile,
            mode=self.mode,
        )

    def _configured_agent(self) -> CodingAgent:
        self._require_open()
        if self._agent is not None:
            return self._agent
        try:
            self._agent = CodingKitFactory(self.agent_factory).create(self._config())
        except CodingSimpleAPIError:
            raise
        except Exception as error:
            raise CodingSimpleAPIError(
                "provider_configuration_error",
                "The selected provider could not be configured. Check its optional extra, "
                "server-side credential, model name, and registry entry.",
            ) from error
        return self._agent

    async def _start(self, objective: str, *, team: bool) -> CodingRunHandle:
        self._require_open()
        selected = CodingPreflightMode.TEAM if team else CodingPreflightMode.WORKFLOW
        report = self.preflight(mode=selected, team=team)
        self._raise_preflight(report)
        execution = self.execution
        assert execution is not None
        trusted_objective = objective.strip()
        if not trusted_objective:
            raise ValueError("objective must not be empty")
        coding_agent: CodingAgent | None = None
        if team:
            assert execution.team is not None
            try:
                pins = execution.team.configured.runtimes["lead"].pins
            except KeyError as error:
                raise CodingSimpleAPIError(
                    "team_not_configured", "The configured coding team has no lead runtime."
                ) from error
        else:
            coding_agent = self._configured_agent()
            pins = coding_agent.configured.pins
        plan = await execution.planner.plan(
            trusted_objective,
            context={
                **dict(execution.planning_context),
                "workspace_ref": self.workspace_ref,
                "mode": "team" if team else "workflow",
            },
        )
        run = await execution.runs.create(
            identity=self.identity,
            autonomy=self.mode.autonomy,
            version_pins=pins,
        )
        fingerprint = execution.protected_input_fingerprint(trusted_objective)
        if team:
            assert execution.team is not None
            outcome = execution.team.launcher.launch(
                configured=execution.team.configured,
                runtime=execution.team.runtime,
                controller=execution.controller,
                run=run,
                identity=self.identity,
                plan=plan,
                standards=execution.standards,
                protected_input_fingerprint=fingerprint,
            )
            reference = (
                f"{execution.team.configured.definition.id}@"
                f"{execution.team.configured.definition.version}"
            )
        else:
            assert coding_agent is not None
            outcome = coding_agent.execute_workflow(
                controller=execution.controller,
                run=run,
                identity=self.identity,
                plan=plan,
                standards=execution.standards,
                protected_input_fingerprint=fingerprint,
            )
            reference = (
                f"{coding_agent.configured.definition.id}@"
                f"{coding_agent.configured.definition.version}"
            )
        task = asyncio.create_task(outcome)
        return CodingRunHandle(
            run_id=run.id,
            objective=trusted_objective,
            mode="team" if team else "workflow",
            agent_or_team_ref=reference,
            workspace_ref=self.workspace_ref,
            identity=self.identity,
            execution=execution,
            task=task,
        )

    def _workflow_preflight(self) -> list[CodingPreflightIssue]:
        issues = self._provider_preflight(requires_structured_output=True)
        assert self.execution is not None
        if not self.execution.standards.valid:
            issues.append(
                CodingPreflightIssue(
                    code="verifier_not_registered",
                    message="The resolved standards contain conflicts.",
                )
            )
        for requirement in self.execution.standards.requirements:
            if (
                requirement.applies is not False
                and self.execution.controller.completion_gate.verifiers.get(
                    requirement.definition.verifier_ref
                )
                is None
            ):
                issues.append(
                    CodingPreflightIssue(
                        code="verifier_not_registered",
                        message=("Register verifier: " + requirement.definition.verifier_ref),
                    )
                )
        return issues

    def _provider_preflight(
        self, *, requires_structured_output: bool
    ) -> list[CodingPreflightIssue]:
        issues: list[CodingPreflightIssue] = []
        if self.provider_preset is None or self.model_capabilities is None:
            issues.append(
                CodingPreflightIssue(
                    code="provider_capabilities_missing",
                    message=(
                        "Register validated provider/model capabilities before action or "
                        "workflow mode."
                    ),
                )
            )
        else:
            compatibility = check_provider_compatibility(
                self.provider_preset,
                capabilities=self.model_capabilities,
                requirement=ProviderRequirement(
                    requires_tools=True,
                    requires_structured_output=requires_structured_output,
                    locality=self.data_locality,
                ),
            )
            issues.extend(
                CodingPreflightIssue(code=item.code, message=item.message)
                for item in compatibility.issues
            )
        return issues

    def _team_preflight(self) -> list[CodingPreflightIssue]:
        assert self.execution is not None and self.execution.team is not None
        binding = self.execution.team
        issues: list[CodingPreflightIssue] = []
        for role, runtime in binding.configured.runtimes.items():
            compatibility = binding.provider_compatibility.get(role)
            if compatibility is None:
                issues.append(
                    CodingPreflightIssue(
                        code="provider_capabilities_missing",
                        message=f"Register validated provider/model capabilities for role: {role}",
                    )
                )
                continue
            if compatibility.preset.provider_ref != runtime.pins.provider_ref:
                issues.append(
                    CodingPreflightIssue(
                        code="provider_not_registered",
                        message=f"Role provider pin does not match capability preset: {role}",
                    )
                )
            for item in compatibility.issues:
                issues.append(
                    CodingPreflightIssue(code=item.code, message=f"Role {role}: {item.message}")
                )
            if (
                self.data_locality is DataLocality.LOCAL_ONLY
                and compatibility.capabilities.data_locality is not DataLocality.LOCAL_ONLY
            ):
                issues.append(
                    CodingPreflightIssue(
                        code="locality_violation",
                        message=f"Role {role} would route local-only data to a cloud provider.",
                    )
                )
        return issues

    @staticmethod
    def _raise_preflight(report: CodingPreflightResult) -> None:
        if report.ok:
            return
        first = report.issues[0]
        raise CodingSimpleAPIError(first.code, first.message)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("CodingKit is closed")


def _lazy_provider_factory(*, provider: str, model: str) -> ProviderFactory:
    normalized = provider.strip().lower()

    def create(instructions: str) -> ModelProvider:
        if normalized == "openai":
            return OpenAIResponsesProvider(model=model, instructions=instructions, store=False)
        if normalized == "gemini":
            return GeminiGenerateContentProvider(model=model, instructions=instructions)
        if normalized == "ollama":
            return OllamaChatProvider(model=model, instructions=instructions)
        raise CodingSimpleAPIError(
            "provider_not_registered",
            "Use openai, gemini, ollama, or supply an explicit provider_factory.",
        )

    return create


def _workspace_ref(path: Path) -> str:
    normalized = str(path).replace("\\", "/").casefold()
    return f"workspace_{_short_hash(normalized)}"


def _short_hash(value: str) -> str:
    from hashlib import sha256

    return sha256(value.encode()).hexdigest()[:16]


def _unique_issues(issues: Sequence[CodingPreflightIssue]) -> list[CodingPreflightIssue]:
    seen: set[tuple[str, str]] = set()
    unique: list[CodingPreflightIssue] = []
    for issue in issues:
        key = (issue.code, issue.message)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique
