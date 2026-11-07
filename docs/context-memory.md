# Context engine and short-term memory

Context building and memory are separate concerns.

- ContextBuilder assembles bounded model context for one request.
- ConversationStore remains the durable source of normalized text and voice messages.
- SessionMemoryManager stores only a compact, expiring session summary.
- Task and job progress do not belong in memory.

`AgentRuntime` can now receive an optional `ContextBuilder`, bounded
`ContextBudget`/`ContextWindow`, and `ContextCompactionManager`. Without those ports,
the chat fast path and legacy `Agent` behavior remain unchanged.

## Context sources

The first implementation includes current input, static system or policy content,
recent conversation, and session summary sources. Future task, job, objective,
knowledge, and long-term memory sources implement the same ContextSource protocol.

Every source has a type-specific token budget and the complete context has a hard
total budget. Required and higher-priority content is allocated first. Optional
source failures are recorded as omitted; required source failures stop the build.

Token accounting uses an injected `TokenCounter`. The dependency-free fallback keeps
the original estimate, while provider adapters can supply a tokenizer through
`CallableTokenCounter`. Output reservations, fixed request overhead, and selected tool
schemas reduce the available input window before the provider is called. Overflow
fails before a model request rather than silently dropping required authority.

Recent conversation retrieval verifies tenant, conversation, and user identity.
Conversation IDs supplied by a client are not sufficient on their own. It selects the
newest messages of the owner (not the first stored ones) and renders them in
chronological order. Stores that implement the optional `ConversationWindowStore`
capability (`list_latest_messages`, `message_exists`; the in-memory and SQLite stores do)
answer this with a bounded query. A store implementing only `ConversationStore` still
works through a forward-paging fallback that reads the whole conversation.
`list_messages` keeps its oldest-first, `after_id` incremental contract.

Under budget pressure, equal-priority items that carry `ContextItem.order` are kept
newest first and then listed oldest first, so the latest turns survive and the final
context stays chronological. Required items and priorities are unchanged.

## Session summaries

SessionSummary is structured and tenant/user/conversation scoped. The manager:

- enforces a maximum estimated token count;
- applies an explicit retention period;
- discards expired summaries on read;
- supports deterministic forget;
- does not call a model or infer durable preferences.

Automatic rolling compaction is a separate injected service. It calls a host-owned
structured `SummaryGenerator` only after a soft threshold, keeps recent raw messages,
validates exact source-message provenance, preserves previous goals, decisions,
constraints and artifact references, rejects detected credentials, and leaves raw
conversation history untouched. Missing source cursors fail closed. Compaction reads
forward from the stored cursor with `after_id`, so it keeps advancing in conversations
longer than 1,000 messages and never skips messages between the cursor and the tail. Each
pass summarizes at most `message_limit` messages; a longer backlog takes several passes.
A store without `message_exists` cannot distinguish a missing cursor from an empty tail
and reports no new messages. Canonical prompts,
policy, task state, acceptance criteria, and permissions never come from this lossy
summary.

Large tool output may be passed through `ArtifactToolResultProcessor`. It writes the
full JSON output to an owner-scoped `ArtifactManager` and gives the model only a small
preview plus reference, checksum, size, and MIME metadata. Missing owner scope fails
closed. This is composed as a `ToolExecutor` decorator so the legacy `Agent` public
signature remains unchanged.

In-memory and SQLite stores are reference implementations. Model-generated
summarization, sensitive-data admission, long-term memory, conflict handling, and
semantic retrieval are deliberately deferred to the long-term memory phase.
