# Workspace Writes and HTML Delivery

Command-approval policy below is superseded by ADR-0013: default offline Docker commands now
execute automatically. Host commands, skill installation and MCP retain confirmation.

## Decision

Docker write_file uses the same workspace rule as host write_file, not mandatory confirmation
for every chunk. Both permission checking and the container worker enforce workspace boundaries.
Escaping paths, credential paths and automatic writes to hidden/internal directories are denied.
Saved agents can still escalate write_file to confirm. Shell execution, installation and MCP
keep their existing confirmation requirements; no run-wide blanket approval is introduced.

Successful HTML writes are tracked by a run-local ArtifactEventBus. Before terminal done, HTML
and directly referenced local stylesheets/classic scripts/images are bundled and stored as a
bounded immutable artifact in MongoDB (2 MB per file, at most 20 HTML entries per run). Successful
approved shell calls also discover non-hidden HTML outputs. Failed/denied writes do not produce
artifacts. Files survive container eviction and server restart. Artifact SSE and done.artifacts
use the existing frontend preview/download contract. Local chat persistence retains references.

Downloads use /api/files/{opaque-id}/download with attachment disposition, no-store, nosniff and
sandbox CSP. The existing frontend sanitizes HTML in an opaque, script-free iframe. Downloading
and opening the file allows scripts; previewing it does not execute generated JavaScript.

## Limits

Bundling covers direct HTML stylesheet/script/image references, not JS module graphs, fetch,
CSS imports or CSS-relative asset URLs. Prefer self-contained CSS and classic JS. This is HTML
delivery, not generic uploads, PDF/Office conversion or a browser execution sandbox.

This remains a trusted single-user loopback deployment. Opaque file IDs are not authorization.
Authentication and owner-scoped artifact lookup belong to the already planned M2A work; do not
expose this endpoint or other administration/runtime endpoints publicly before that work.

## Verification

Unit tests cover bounded bundling, Chinese text, workspace/credential/internal path rejection,
symlink escape, file tracking and denied writes. API tests cover host and real Docker HTML/CSS/JS
generation without approval prompts, download headers/content, unknown files, and persistence
across app restart. Explicit confirm policies and shell/MCP/install approvals remain covered.

## Amendment (2026-10-29): delivery covers the document formats, not just HTML

A PDF the agent rendered or a deck it built stayed invisible: only `.html/.htm`
made the trip. The delivery now covers every format the browser can show one way
or another -- PDF pages via pdf.js, images as they are, and the Office formats
through a bounded read of their content (sheets, slides with titles and points,
document paragraphs) extracted in `previews.py` with nothing beyond `zipfile`
and `ElementTree`, because the sandbox has no network to install `openpyxl` or
`python-pptx` with.

The scan in `done` now walks the whole workspace for these suffixes, newest
first; images are capped at 6 per turn so a batch of charts cannot crowd the
deck out. A file whose bytes are unchanged since an earlier turn is delivered by
reference -- the card reappears under the newer message pointing at the stored
delivery, and nothing is stored twice. The download endpoint now sends each
delivery under its real MIME type, and the sandbox CSP applies to HTML only;
PDF and images are rendered by our viewer from bytes that never act as a
document of their own.
