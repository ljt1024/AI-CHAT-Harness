# ADR-0019: Retry a failed model call, and let the user retry a failed turn

Date: 2026-11-03
Status: Accepted
Builds on: ADR-0003 (runtime and checkpoints), ADR-0011 (model output recovery)

## Context

A deployment went live and the first real failures were all transient: `429`
while the provider throttled us, `500 Service is too busy` while it rebalanced.
Nothing about the request was wrong, and nothing in the code gave it a second
chance — `httpx`'s `raise_for_status()` turned the first non-2xx answer into a
failed turn, and the gateway's only recovery was a *provider* fallback, which
exists for a different reason (a provider that is down, not one that is busy).

The user-visible half of the same gap was worse. When a turn failed, the
assistant bubble rendered the error text and nothing else: the footer that
holds the retry button was wrapped in `{!msg.isError && ...}`, so the retry
control disappeared exactly when it was needed. The only way forward was to
retype the question, which also costs another turn against the guest budget.

## Decision

**Retry in two places, for two different reasons.**

1. **The gateway repeats the request itself** (`gateway/retry.py`,
   `ModelGateway._stream_with_retries`), `MODEL_RETRIES` times (default 2) with
   exponential backoff `MODEL_RETRY_BACKOFF * 2^n`, capped by
   `MODEL_RETRY_MAX_BACKOFF`, and honouring the provider's `Retry-After` when
   it sends one. Only failures that say nothing about the request are repeated:
   `408/409/425/429/500/502/503/504` and transport errors. A `400/401/402/403/
   404/422` is re-raised immediately — repeating it only spends the user's turn
   to arrive at the same refusal.

2. **The UI keeps a retry control on a failed turn** (`MessageItem`). The error
   row gets its own labelled button, outside the block that is hidden on error.
   It reuses the existing handler that drops the failed answer and replays the
   last user message, so a retry costs one click instead of a retyped prompt.

## The invariant that makes this safe

**Never replay a request that already produced output.** A streamed answer is
not idempotent: the reader has the text, and a second attempt would append a
duplicate. Both layers stop at the first chunk —

- the gateway marks `emitted` as soon as one chunk is yielded and re-raises
  instead of sleeping, so a mid-stream `503` stays a failure;
- the provider fallback keeps its original guard, so a partial stream is never
  replayed against another model either.

A retry is therefore a *pre-first-token* concern only. Everything that happens
after the first token is ADR-0011's job: the model got an answer out, and the
recovery question is how to make it a complete one, not whether to ask again.

## Consequences

- A transient provider blip is now invisible: the user waits one or two extra
  seconds instead of seeing a failed turn.
- The retry budget is per model call, not per turn. A ten-step agent run may
  retry ten times; each retry costs at most the configured backoff, and the
  wall-clock ceiling is bounded by `MODEL_RETRIES * MODEL_RETRY_MAX_BACKOFF`.
- Retries multiply provider spend in proportion to how flaky the provider is.
  That is acceptable because a retry only happens when the previous attempt
  produced no tokens, so no output is paid for twice.
- When every attempt fails, the runtime now says *which* failure it was
  (`429` vs `402` vs a dropped connection) instead of "run failed". The trace
  records `error_code = model_unavailable` so these are countable in logs.
- `ModelGateway()` defaults to zero retries. A hand-built gateway is a test
  double, and a test that fails on purpose must not spend seconds sleeping.
  Only `create_app` passes the configured budget in.
- Still missing: if the provider drops the connection *after* the first token,
  the user sees a truncated answer and must press retry — the same as before,
  now at least with a button.
