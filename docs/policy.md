# Policy engine

The central policy engine evaluates framework-neutral policy rules before an action
crosses an execution boundary. Product applications register their own rules; the
reusable package contains only generic capability, runtime, scheduling, delegation,
and tool adapters.

## Decision precedence

The engine resolves matching decisions in this order:

deny, require approval, review, modify, allow.

No matching policy is denied by default. Tool execution always supplies built-in
permission and approval policies, which preserves the existing tool behavior while
allowing additional product policy to make the result stricter.

## Tool execution order

1. Validate model-generated input.
2. Evaluate tool permissions and registered policy.
3. Revalidate any policy-modified arguments.
4. Resolve approval.
5. Perform idempotency lookup.
6. Execute with timeout and retry.
7. Persist idempotency result and emit audit/events.

A deny decision always wins. A custom policy cannot use allow or modify to bypass a
missing permission or required approval.

## Idempotency and autonomous side effects

Identical scoped operations (same owner, tool, and key) execute once per process: a
concurrent duplicate waits for and shares the first result. Reusing a key with different
canonical arguments raises `IdempotencyConflictError`, in flight, after completion, and
with the durable action-intent store. The durable store's atomic claim provides
exclusion across processes, and an uncertain outcome is never replayed.
`AgentRuntime` raises `DurableSideEffectsRequiredError` before any model call when
`AutonomyLevel.AUTONOMOUS` exposes a side-effecting tool and the `ToolExecutor` has no
durable `action_intent_store`.

## Recoverable tool errors

By default any tool error ends the run. `Agent(tool_errors="return")` and
`AgentRuntime(tool_errors="return")` instead return a sanitized
`ToolResult(is_error=True)` for invalid arguments (location, type, and message only,
never input values), unknown tools, and failures of read-only tools, up to
`max_tool_errors` per run (default 3). Permission, policy, approval, path-policy,
idempotency-conflict, and side-effecting tool failures always stay fatal.

## Product composition

Consumers create a PolicyRegistry, register product-neutral policy instances or
product-owned PolicyRule implementations, then inject PolicyEngine into ToolExecutor.
Tenant, user, agent, session, conversation, and run identity must originate from
trusted application context rather than model arguments.

Policy decisions are written through the existing redacting AuditStore and emitted
through canonical policy event types. Product HTTP routes, policy configuration
files, and domain-specific rules remain outside this package.
