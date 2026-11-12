# 0011: Recover bounded model output failures before tool execution

Status: Accepted

Context: Real HTML generation reached the adapter's 4096-token output limit. The returned
write_file arguments were incomplete JSON, causing JSONDecodeError and a generic run failure.

Decision: Request up to 8192 output tokens by default, preserving explicit model limits.
Inspect finish_reason before accepting any calls. Reject length-limited responses even if a
JSON prefix happens to be valid, and validate the entire tool-call batch before emitting calls.
Malformed arguments become a sanitized ModelOutputError, not raw parser diagnostics.

The runtime permits at most two output-recovery attempts within the existing turn and cost
limits. It discards every call from the rejected response, adds explicit smaller-output
feedback, and retains successful earlier tool results without replay. Recovery counts survive
checkpoints. Do not apply this mechanism to arbitrary tool execution or transport failures.
Usage from failed model responses is still charged to the run's budget.

write_file retains replace-by-default behavior and adds an explicit append flag for later
chunks. Recommend separate HTML/CSS/JS files or chunks under 6000 characters. Docker writes,
including appends, retain human approval. Import, path guards and credential protections do
not change. Partial completed files can remain after a later failure; do not claim the whole
artifact succeeded or automatically replay append operations.

Full-task regression also exposed ambiguous file-read units: the model treated byte offsets
as line numbers and exhausted its turn limit re-reading skill instructions. read_file now
documents and implements character offsets, provides bounded 1200-character pages with exact
next_offset/EOF, and uses UTF-8 JSON in the Docker worker so Chinese pages fit the transport.
This corrects multibyte positioning without changing ASCII offsets or whole small-file reads.
The last three model calls receive an explicit remaining-turn/finalization reminder, avoiding
over-checking until an empty final result. Non-completed stop paths return an honest limit
message rather than an empty answer. Sandbox tool descriptions identify the actual Python-only
environment, and denied-action feedback explicitly forbids repeating a declined action.

Record rejected-turn metadata, actual model input, usage, error code and finish reason without
logging malformed argument contents. Emit a recovery trace step; terminal errors are actionable
and follow Chinese task language where applicable. Plain chat reports truncation as terminal
SSE error instead of a successful completion or an uncaught streaming exception.

Verification: Adapter tests cover fragmented JSON, truncation, valid-prefix rejection and
all-or-nothing batches. Runtime tests cover bounded retries, cost/turn caps, checkpoint resume
and no duplicate writes. Real Docker tests require approval after recovery and confirm denied
writes do not occur. scripts/verify_html_generation.py replays the original task in a separate
session against the real provider; it does not alter the source conversation.
