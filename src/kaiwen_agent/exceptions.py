class AgentError(Exception):
    """Base error for predictable runtime failures."""


class DuplicateToolError(AgentError):
    """Raised when two tools share the same public name."""


class PermissionDeniedError(AgentError):
    """Raised before a tool executes without all required permissions."""


class ToolNotFoundError(AgentError):
    """Raised when a model requests an unregistered tool."""


class ToolValidationError(AgentError):
    """Raised when tool arguments fail schema validation."""


class ToolArgumentsError(ToolValidationError):
    """Raised when model-supplied arguments fail the tool input schema."""


class ApprovalRequiredError(AgentError):
    """Raised when an approval-protected tool has no approval gate."""


class DurableApprovalRequiredError(ApprovalRequiredError):
    """Raised with the durable approval identity a host must resolve."""

    def __init__(self, message: str, *, approval_id: str) -> None:
        super().__init__(message)
        self.approval_id = approval_id


class ApprovalDeniedError(AgentError):
    """Raised when a tool execution request is rejected."""


class ToolTimeoutError(AgentError):
    """Raised when a tool exceeds its declared timeout."""


class ToolExecutionError(AgentError):
    """Raised when a tool handler fails after its retry policy is exhausted."""


class ActionOutcomeUncertainError(ToolExecutionError):
    """Raised when a prior external side effect cannot safely be retried."""

    def __init__(self, message: str, *, action_intent_id: str) -> None:
        super().__init__(message)
        self.action_intent_id = action_intent_id


class IdempotencyConflictError(AgentError, ValueError):
    """Raised when an idempotency key is reused with different canonical arguments.

    It is also a ValueError so callers that caught the store's earlier binding error keep working.
    """


class RunOwnershipError(AgentError):
    """Raised when a workflow run is owned by another executor or ownership was lost."""


class OwnershipLostError(RunOwnershipError):
    """A fenced write was refused by the authoritative lease state.

    `code` is stable and non-sensitive. The message never carries lease credentials, SQL text or
    storage error messages.
    """

    def __init__(self, code: str) -> None:
        super().__init__(f"Fenced write refused: {code}")
        self.code = code


class FencedConfigurationError(AgentError):
    """Fenced execution was requested for a configuration that cannot enforce it."""


class ActionLookupIncompleteError(AgentError):
    """Raised when in-flight action intents cannot be listed completely."""


class DurableSideEffectsRequiredError(AgentError):
    """Raised when autonomous side effects are requested without durable coordination."""


class ModelResponseError(AgentError):
    """Raised when a provider returns a malformed response, such as invalid tool arguments."""
