import type { AgentArtifact, AgentEvent } from "@imthekaiwen/agent-protocol";

export type IslandMode =
  | "idle"
  | "listening"
  | "composing"
  | "thinking"
  | "tool_activity"
  | "waiting_approval"
  | "waiting_user"
  | "result"
  | "compact";

export type ConnectionState = "disconnected" | "connecting" | "connected";

export interface AgentUIState {
  sessionId: string | null;
  connection: ConnectionState;
  mode: IslandMode;
  text: string;
  artifacts: AgentArtifact[];
  activeTool?: string;
  progress?: number;
  error?: string;
  lastEvent?: AgentEvent;
}

export const initialAgentUIState: AgentUIState = {
  sessionId: null,
  connection: "disconnected",
  mode: "idle",
  text: "",
  artifacts: [],
};

const islandModes = new Set<IslandMode>([
  "idle",
  "listening",
  "composing",
  "thinking",
  "tool_activity",
  "waiting_approval",
  "waiting_user",
  "result",
  "compact",
]);

export function reduceAgentEvent(state: AgentUIState, event: AgentEvent): AgentUIState {
  const payload = event.payload;
  const base = { ...state, lastEvent: event, error: undefined };
  switch (event.type) {
    case "conversation.delta":
      return {
        ...base,
        mode: "result",
        text: state.text + stringValue(payload.delta),
      };
    case "conversation.completed":
    case "run.completed":
      return {
        ...base,
        mode: "result",
        text: typeof payload.text === "string" ? payload.text : state.text,
        activeTool: undefined,
        progress: undefined,
      };
    case "run.started":
      return { ...base, mode: "thinking", text: "", artifacts: [] };
    case "tool.started":
      return {
        ...base,
        mode: "tool_activity",
        activeTool: stringValue(payload.tool) || undefined,
      };
    case "tool.completed":
      return { ...base, mode: "thinking", activeTool: undefined };
    case "tool.failed":
      return {
        ...base,
        mode: "result",
        activeTool: undefined,
        error: stringValue(payload.message) || "Tool operation failed",
      };
    case "approval.requested":
      return { ...base, mode: "waiting_approval" };
    case "approval.resolved":
      return { ...base, mode: "thinking" };
    case "task.started":
    case "task.queued":
    case "task.retrying":
      return { ...base, mode: "tool_activity" };
    case "task.progress":
      return {
        ...base,
        mode: "tool_activity",
        progress: numberValue(payload.progress),
      };
    case "task.completed":
      return { ...base, mode: "result", progress: 1 };
    case "run.cancelled":
      return {
        ...base,
        mode: "result",
        activeTool: undefined,
        progress: undefined,
        error: stringValue(payload.message) || "Run cancelled",
      };
    case "task.cancelled":
      return {
        ...base,
        mode: "result",
        error: stringValue(payload.message) || "Task cancelled",
      };
    case "run.failed":
    case "task.failed":
      return {
        ...base,
        mode: "result",
        error: stringValue(payload.message) || "Agent operation failed",
      };
    case "artifact.created": {
      const artifact = artifactValue(payload.artifact);
      return artifact
        ? { ...base, mode: "result", artifacts: [...state.artifacts, artifact] }
        : base;
    }
    case "client.state": {
      const mode = payload.mode;
      return typeof mode === "string" && islandModes.has(mode as IslandMode)
        ? { ...base, mode: mode as IslandMode }
        : base;
    }
    default:
      return base;
  }
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && value >= 0 && value <= 1 ? value : undefined;
}

function artifactValue(value: unknown): AgentArtifact | undefined {
  if (typeof value !== "object" || value === null) return undefined;
  const artifact = value as Partial<AgentArtifact>;
  if (
    typeof artifact.id !== "string" ||
    typeof artifact.type !== "string" ||
    typeof artifact.data !== "object" ||
    artifact.data === null
  ) {
    return undefined;
  }
  return artifact as AgentArtifact;
}
