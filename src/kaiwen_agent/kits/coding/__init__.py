"""First-party, opt-in professional coding composition."""

from kaiwen_agent.kits.coding.discovery import (
    CodingProjectSnapshot,
    DiscoverySource,
    ProjectDiscovery,
)
from kaiwen_agent.kits.coding.documentation import (
    DocumentationAction,
    DocumentationProposal,
    DocumentationTier,
    classify_documentation_tier,
    propose_documentation,
)
from kaiwen_agent.kits.coding.facade import CodingKit, SimpleCodingAgent, SimpleCodingTeam
from kaiwen_agent.kits.coding.factory import (
    CodingAgent,
    CodingKitConfig,
    CodingKitFactory,
    CodingPreflightIssue,
    CodingPreflightResult,
)
from kaiwen_agent.kits.coding.handles import CodingRunHandle, CodingRunResult
from kaiwen_agent.kits.coding.observability import (
    CodingEventEmitter,
    CodingEventKind,
    CodingRunManifest,
    persist_run_manifest,
)
from kaiwen_agent.kits.coding.planner import (
    CodingPlanningError,
    ObjectivePlanner,
    PlanningConstraints,
    StructuredPlanningBackend,
)
from kaiwen_agent.kits.coding.profiles import (
    CodingMode,
    CodingProfile,
    CodingProfileSettings,
    coding_profile_settings,
)
from kaiwen_agent.kits.coding.prompting import (
    PROMPT_VERSION,
    CodingConversationPreferences,
    CodingUserLanguage,
    coding_prompt_definitions,
    install_coding_prompts,
)
from kaiwen_agent.kits.coding.providers import (
    CodingProviderCatalog,
    CodingProviderPreset,
    ProviderCompatibility,
    ProviderCompatibilityIssue,
    ProviderRequirement,
    RoleUsageSummary,
    check_provider_compatibility,
    register_coding_provider,
)
from kaiwen_agent.kits.coding.revision import BoundedCodingRevisionPlanner
from kaiwen_agent.kits.coding.simple_config import (
    CodingExecutionBinding,
    CodingPreflightMode,
    CodingRunControl,
    CodingSimpleAPIError,
    CodingTeamBinding,
    CodingTeamLauncher,
)
from kaiwen_agent.kits.coding.standards import (
    coding_standards_packs,
    install_coding_standards,
)
from kaiwen_agent.kits.coding.team import (
    CodingRoleConfig,
    CodingTeamConfig,
    CodingTeamFactory,
    ConfiguredCodingTeam,
    select_coding_roles,
)
from kaiwen_agent.kits.coding.verification import (
    JsonArtifactVerifier,
    coding_verifiers,
    install_coding_verifiers,
)

__all__ = [
    "PROMPT_VERSION",
    "CodingProjectSnapshot",
    "CodingAgent",
    "CodingKitConfig",
    "CodingKitFactory",
    "CodingKit",
    "CodingExecutionBinding",
    "CodingEventEmitter",
    "CodingEventKind",
    "CodingMode",
    "CodingPlanningError",
    "CodingPreflightIssue",
    "CodingPreflightResult",
    "CodingPreflightMode",
    "CodingProfile",
    "CodingProfileSettings",
    "CodingProviderCatalog",
    "CodingProviderPreset",
    "CodingRoleConfig",
    "CodingRunManifest",
    "CodingRunControl",
    "CodingRunHandle",
    "CodingRunResult",
    "CodingSimpleAPIError",
    "CodingTeamConfig",
    "CodingTeamFactory",
    "CodingTeamBinding",
    "CodingTeamLauncher",
    "ConfiguredCodingTeam",
    "CodingConversationPreferences",
    "CodingUserLanguage",
    "DiscoverySource",
    "DocumentationAction",
    "DocumentationProposal",
    "DocumentationTier",
    "JsonArtifactVerifier",
    "ObjectivePlanner",
    "PlanningConstraints",
    "ProviderCompatibility",
    "ProviderCompatibilityIssue",
    "ProviderRequirement",
    "ProjectDiscovery",
    "StructuredPlanningBackend",
    "RoleUsageSummary",
    "SimpleCodingAgent",
    "SimpleCodingTeam",
    "BoundedCodingRevisionPlanner",
    "classify_documentation_tier",
    "coding_prompt_definitions",
    "coding_profile_settings",
    "check_provider_compatibility",
    "coding_standards_packs",
    "coding_verifiers",
    "install_coding_prompts",
    "install_coding_standards",
    "install_coding_verifiers",
    "persist_run_manifest",
    "propose_documentation",
    "register_coding_provider",
    "select_coding_roles",
]
