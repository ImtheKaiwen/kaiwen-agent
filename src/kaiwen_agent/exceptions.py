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
