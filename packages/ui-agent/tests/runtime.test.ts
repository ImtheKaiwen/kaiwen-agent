import assert from "node:assert/strict";
import test from "node:test";

import type { AgentEvent, AgentEventType } from "@imthekaiwen/agent-protocol";

import {
  ActionRegistry,
  AgentUIRuntime,
  type AgentTransport,
  type ClientActionResult,
  type ClientCapabilities,
  type EventHandler,
} from "../src/index.js";

class FakeTransport implements AgentTransport {
  handler?: EventHandler;
  inputs: string[] = [];
  results: ClientActionResult[] = [];
  capabilities?: ClientCapabilities;
  closed = false;

  async connect(_sessionId: string, handler: EventHandler) {
    this.handler = handler;
    return () => {
      this.closed = true;
    };
  }

  async sendInput(_sessionId: string, text: string) {
    this.inputs.push(text);
  }

  async sendActionResult(
    _sessionId: string,
    _traceId: string,
    result: ClientActionResult,
  ) {
    this.results.push(result);
  }

  async sendCapabilities(_sessionId: string, capabilities: ClientCapabilities) {
    this.capabilities = capabilities;
  }
}

function event(
  type: AgentEventType,
  payload: Record<string, unknown> = {},
  id = `evt_${Math.random().toString(36).slice(2)}`,
): AgentEvent {
  return {
    schema_version: "1.0",
    id: id as `evt_${string}`,
    type,
    timestamp: "2026-09-17T12:00:00.000Z",
    trace_id: "trace-1",
    session_id: "session-1",
    payload,
  };
}

test("runtime connects, advertises capabilities, and follows island states", async () => {
  const transport = new FakeTransport();
  const actions = new ActionRegistry().register<{ path: string }, { opened: string }>({
    name: "navigate",
    schema: {
      type: "object",
      additionalProperties: false,
      required: ["path"],
      properties: { path: { type: "string" } },
    },
    handler: ({ path }) => ({ opened: path }),
  });
  const runtime = new AgentUIRuntime(transport, actions, ["project_card"]);

  await runtime.connect("session-1");
  assert.equal(runtime.getState().connection, "connected");
  assert.equal(runtime.getState(), runtime.getState());
  assert.deepEqual(transport.capabilities, {
    actions: [
      {
        name: "navigate",
        schema: {
          type: "object",
          additionalProperties: false,
          required: ["path"],
          properties: { path: { type: "string" } },
        },
      },
    ],
    artifacts: ["project_card"],
  });

  await runtime.submit("Show products");
  assert.deepEqual(transport.inputs, ["Show products"]);
  assert.equal(runtime.getState().mode, "thinking");

  await runtime.handleEvent(event("tool.started", { tool: "catalog.search" }));
  assert.equal(runtime.getState().mode, "tool_activity");
  assert.equal(runtime.getState().activeTool, "catalog.search");

  await runtime.handleEvent(event("task.progress", { progress: 0.5 }));
  assert.equal(runtime.getState().progress, 0.5);

  await runtime.handleEvent(
    event("artifact.created", {
      artifact: { id: "project-1", type: "project_card", data: { title: "Cube Rivals" } },
    }),
  );
  assert.equal(runtime.getState().artifacts[0]?.type, "project_card");

  await runtime.handleEvent(
    event("client_action.requested", {
      action: { id: "action-1", name: "navigate", arguments: { path: "/products" } },
    }),
  );
  assert.deepEqual(transport.results[0], {
    action_id: "action-1",
    name: "navigate",
    ok: true,
    data: { opened: "/products" },
  });

  const completed = event("run.completed", { text: "Here are the products." }, "evt_done");
  await runtime.handleEvent(completed);
  await runtime.handleEvent(completed);
  assert.equal(runtime.getState().text, "Here are the products.");
  assert.equal(runtime.getState().mode, "result");

  runtime.disconnect();
  assert.equal(transport.closed, true);
  assert.equal(runtime.getState().sessionId, null);
});

test("a cancelled run is a result state with its own message, distinct from failure", async () => {
  const transport = new FakeTransport();
  const runtime = new AgentUIRuntime(transport, new ActionRegistry(), []);
  await runtime.connect("session-1");
  await runtime.handleEvent(event("run.started", {}));
  await runtime.handleEvent(event("run.cancelled", {}, "evt_cancelled"));
  assert.equal(runtime.getState().mode, "result");
  assert.equal(runtime.getState().error, "Run cancelled");
  assert.equal(runtime.getState().activeTool, undefined);
});
