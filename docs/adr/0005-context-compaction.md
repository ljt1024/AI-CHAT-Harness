# 0005: Bounded tool output and batched context compaction
Background: Long tool results and histories degrade model reliability and exceed context windows.
Decision: Bound tool text to 4,000 characters with an actionable truncation hint.
Amendment (2026-10-29): a tool may raise its own ceiling through `ToolResult.limit`; remote MCP tools declare 16,000. A remote call usually returns one document (a product listing, a page of JSON), and clipping it at the local default left the model a half string it could not parse. Both the registry and the middleware read that declared limit, so the small default is applied once, not twice. Local tools keep 4,000.
Decision: Estimate tokens conservatively from UTF-8 bytes; compact at 70% of the configured window.
Decision: Summarize early history in bounded batches; preserve recent messages and complete tool pairs.
Decision: Charge summary token usage to the same run and stop when its budget is exhausted.
Alternative: Drop old messages or split arbitrary tool conversations; rejected as unsafe for continuity.
Consequence: If recent context or a summary cannot fit safely, fail explicitly instead of dropping data.
Consequence: A summary that overshoots its character budget is clipped at a sentence or paragraph break, not treated as a failed run - a live transcript was killed by 6016 characters against a 6000 budget, which no user can act on. An empty reply is retried against the two halves of that batch before the run gives up, and the final error names the cause instead of a batch size.
Consequence: A compaction emits a `memory` event with the summarized and retained counts, so the transcript shown to the user says that older turns became a summary rather than silently changing what the model can see.
Consequence: Each orderly stop commits the final window reading with the session document and serves it at `GET /api/sessions/{id}/context`, so a reloaded page restores the composer gauge without replaying the run's events.
