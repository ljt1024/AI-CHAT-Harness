# 0005: Bounded tool output and batched context compaction
Background: Long tool results and histories degrade model reliability and exceed context windows.
Decision: Bound tool text to 4,000 characters with an actionable truncation hint.
Decision: Estimate tokens conservatively from UTF-8 bytes; compact at 70% of the configured window.
Decision: Summarize early history in bounded batches; preserve recent messages and complete tool pairs.
Decision: Charge summary token usage to the same run and stop when its budget is exhausted.
Alternative: Drop old messages or split arbitrary tool conversations; rejected as unsafe for continuity.
Consequence: If recent context or a summary cannot fit safely, fail explicitly instead of dropping data.
