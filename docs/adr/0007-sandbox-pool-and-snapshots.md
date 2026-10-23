# 0007: Pool ownership and bounded snapshots
Background: Reusing containers must not mix session files; recovery needs durable workspace data.
Decision: Warm containers have no owner; claimed and cached containers belong to one session only.
Decision: Rebuilding a cached session reuses its assigned workspace; other sessions never inherit it.
Decision: Three failures open the breaker; recovery permits one probe before closing.
Decision: Store bounded regular-file tar snapshots in GridFS with a SHA256 digest.
Decision: Restore rejects links, devices, absolute paths, traversal and secret paths before writing.
Alternative: Extract arbitrary archives directly into a live workspace; rejected.
Consequence: Snapshot/restore callers must hold exclusive ownership and stop container writes.
