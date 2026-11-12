# 0033 — The reference tree retires

Date: 2026-11-12
Status: Accepted

## Context

ADR-0001 kept `end/` — the Express + LangGraph backend that shipped with
AI-CHAT 2.1.1 — as a read-only reference while the Python harness was written.
The reasons were concrete: the checkpoint semantics (`memoryStore.js`), the
batched compaction idea (`contextMemory.js`), the per-process concurrency guard
(`agentService.js`) and the SSE contract (`routes/`) had to be reproduced, and
keeping the source in the tree meant comparing answers instead of guessing them.

All four now exist in `harness/`, are covered by tests, and are documented
(ADR-0002 for the contract, ADR-0003 for checkpoints, ADR-0005 for compaction).
The tree has not been run since the cutover and nothing in `src/` or `harness/`
imports it. What it still costs:

- 56 tracked files with their own lockfile and a 16 MB font download note.
- `scripts/check-i18n.mjs` reaching into `end/src/i18n/locales` to compare six
  frontend `server.*` strings against a backend nobody serves.
- A README appendix (roughly a third of the file) documenting a product this
  repository no longer is: port 3001, `VITE_CHAT_BASE_URL`, OSS keys, PM2.
- A standing instruction in the task book that the directory must not be
  touched, which reads to anyone new as "there are two runtimes here".

## Decision

1. Remove `end/` from the working tree and the index. History keeps it:
   `git show <commit>:end/...` still answers, and the import commit `fc78865` is
   the authoritative copy.
2. `scripts/check-i18n.mjs` checks only `src/app/i18n/locales`. The six
   `server.*` strings it compared existed to mirror the old backend's messages;
   with no reader and no producer left in the harness they are deleted rather
   than re-pointed at a backend that has no locale directory.
3. `.gitignore` and the ESLint ignore list no longer mention the tree, the
   README drops the appendix, and `ARCHITECTURE.md` names the harness modules
   that now own those responsibilities.

## Consequences

One runtime. Build, lint and `check:i18n` no longer depend on a directory that
was never built, and the i18n check cannot fail for a reason that no longer
exists.

The old backend stays readable but is no longer runnable without restoring it
from history, together with its Node 22 toolchain and fonts. That is deliberate:
it has not been a product since the Python harness took the port, and a
reference that must be restored is still a reference — this one is one
`git checkout` away.

The SSE contract is unchanged: ADR-0002 is the contract and `harness/tests` is
what enforces it, not a side-by-side run of a second server.
