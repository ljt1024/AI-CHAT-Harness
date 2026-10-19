# 0003: Runtime ownership and committed memory
Background: The old backend only commits successful memory and guards concurrent sessions.
Decision: One Python process owns active sessions, the model gateway, tools and approval futures.
Decision: Persist every model turn and tool result; checkpoint at five-turn boundaries and completion.
Decision: Commit session history only after successful completion; reject duplicate committed turn IDs.
Decision: Resume a matching agent from its latest checkpoint with cumulative cost and trace ID.
Alternative: Distributed workers and Redis; deferred until real concurrency requirements exist.
Consequence: Multi-worker Uvicorn deployments are unsupported in Tier 1.
Consequence: Workspace snapshots and crash-safe approval recovery require Tier 2.
