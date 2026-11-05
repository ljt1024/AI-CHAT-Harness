# 0003: Runtime ownership and committed memory
Background: The old backend only commits successful memory and guards concurrent sessions.
Decision: One Python process owns active sessions, the model gateway, tools and approval futures.
Decision: Persist every model turn and tool result; checkpoint at five-turn boundaries and completion.
Decision: Commit session history at every orderly stop — completion, budget, turn or idle limit — cut at the last boundary where each assistant tool call still has its result. An interrupted turn must not lose its work, so an orderly stop keeps what it learned.
Decision: Leave history untouched after a crash or cancellation; those recover from a checkpoint, since the tail of such a transcript is half-written. Reject duplicate committed turn IDs.
Decision: Resume a matching agent from its latest checkpoint with cumulative cost and trace ID; `POST /api/agents/run` takes `resume` to ask for it.
Alternative: Distributed workers and Redis; deferred until real concurrency requirements exist.
Consequence: Multi-worker Uvicorn deployments are unsupported in Tier 1.
Consequence: Workspace snapshots and crash-safe approval recovery require Tier 2.
