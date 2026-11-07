export const SCHEMA_VERSION = "1.0" as const;

export const AGENT_EVENT_TYPES = [
  "conversation.input",
  "conversation.delta",
  "conversation.completed",
  "run.started",
  "run.status",
  "run.completed",
  "run.failed",
  "run.cancelled",
  "tool.started",
  "tool.completed",
  "tool.failed",
  "task.created",
  "task.queued",
  "task.started",
  "task.progress",
  "task.retrying",
  "task.paused",
  "task.resumed",
  "task.completed",
  "task.failed",
  "task.cancelled",
  "job.created",
  "job.queued",
  "job.assigned",
  "job.started",
  "job.progress",
  "job.waiting",
  "job.waiting_approval",
  "job.completed",
  "job.failed",
  "job.cancel_requested",
  "job.cancelled",
  "resource.waiting",
  "resource.acquired",
  "resource.released",
  "approval.requested",
  "approval.resolved",
  "artifact.created",
  "client.connected",
  "client.capabilities",
  "client.state",
  "client_action.requested",
  "client_action.result",
  "client_action.failed",
  "lifecycle.wakeup.scheduled",
  "lifecycle.wakeup.cancelled",
  "lifecycle.cycle.started",
  "lifecycle.cycle.observing",
  "lifecycle.cycle.thinking",
  "lifecycle.cycle.acting",
  "lifecycle.cycle.persisting",
  "lifecycle.cycle.scheduling",
  "lifecycle.cycle.sleeping",
  "lifecycle.cycle.completed",
  "lifecycle.cycle.failed",
  "policy.allowed",
  "policy.denied",
  "policy.approval_required",
  "policy.review_required",
  "policy.modified",
  "trigger.event.accepted",
  "trigger.event.deduplicated",
  "trigger.wakeup.scheduled",
  "objective.created",
  "objective.status.changed",
  "watch.created",
  "watch.matched",
  "watch.expired",
  "watch.cancelled",
  "attention.created",
] as const;

export type AgentEventType =
  | "conversation.input"
  | "conversation.delta"
  | "conversation.completed"
  | "run.started"
  | "run.status"
  | "run.completed"
  | "run.failed"
  | "run.cancelled"
  | "tool.started"
  | "tool.completed"
  | "tool.failed"
  | "task.created"
  | "task.queued"
  | "task.started"
  | "task.progress"
  | "task.retrying"
  | "task.paused"
  | "task.resumed"
  | "task.completed"
  | "task.failed"
  | "task.cancelled"
  | "job.created"
  | "job.queued"
  | "job.assigned"
  | "job.started"
  | "job.progress"
  | "job.waiting"
  | "job.waiting_approval"
  | "job.completed"
  | "job.failed"
  | "job.cancel_requested"
  | "job.cancelled"
  | "resource.waiting"
  | "resource.acquired"
  | "resource.released"
  | "approval.requested"
  | "approval.resolved"
  | "artifact.created"
  | "client.connected"
  | "client.capabilities"
  | "client.state"
  | "client_action.requested"
  | "client_action.result"
  | "client_action.failed"
  | "lifecycle.wakeup.scheduled"
  | "lifecycle.wakeup.cancelled"
  | "lifecycle.cycle.started"
  | "lifecycle.cycle.observing"
  | "lifecycle.cycle.thinking"
  | "lifecycle.cycle.acting"
  | "lifecycle.cycle.persisting"
  | "lifecycle.cycle.scheduling"
  | "lifecycle.cycle.sleeping"
  | "lifecycle.cycle.completed"
  | "lifecycle.cycle.failed"
  | "policy.allowed"
  | "policy.denied"
  | "policy.approval_required"
  | "policy.review_required"
  | "policy.modified"
  | "trigger.event.accepted"
  | "trigger.event.deduplicated"
  | "trigger.wakeup.scheduled"
  | "objective.created"
  | "objective.status.changed"
  | "watch.created"
  | "watch.matched"
  | "watch.expired"
  | "watch.cancelled"
  | "attention.created";

export interface AgentEvent<TPayload extends Record<string, unknown> = Record<string, unknown>> {
  schema_version: typeof SCHEMA_VERSION;
  id: `evt_${string}`;
  type: AgentEventType;
  timestamp: string;
  trace_id: string;
  session_id: string;
  tenant_id?: string | null;
  user_id?: string | null;
  payload: TPayload;
}

export type ArtifactType =
  | "project_card"
  | "task_progress"
  | "approval_card"
  | "form_draft"
  | "notification"
  | "link";

export interface AgentArtifact<TData extends Record<string, unknown> = Record<string, unknown>> {
  id: string;
  type: ArtifactType;
  data: TData;
}

export interface ClientAction<TArguments extends Record<string, unknown> = Record<string, unknown>> {
  id: string;
  name: string;
  arguments: TArguments;
  timeout_ms?: number;
}
