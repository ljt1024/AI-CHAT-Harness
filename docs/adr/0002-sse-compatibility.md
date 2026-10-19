# 0002: HTTP and SSE contract mapping
Background: AI-CHAT uses POST /api/agents/run with data-only SSE frames.
Decision: Keep sessionId and turnId; carry trace_id on every runtime event.
Mapping: turn_started -> start; tool_call/tool_result -> step; content -> delta.
Mapping: done -> done {result}; failures -> error {message}.
Decision: Native events retain their names on GET /api/sessions/{id}/events.
Decision: HTTP/SSE parsing lives in infrastructure; features own domain types.
Alternative: Replace the frontend protocol; rejected to preserve the base.
Consequence: Tier 1 stub is explicitly opt-in; production never fabricates model output.
