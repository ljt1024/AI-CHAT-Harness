# ADR-0020: A file system the reader can put files into

Date: 2026-11-04
Status: Accepted
Builds on: ADR-0003 (runtime and checkpoints), ADR-0009 (sandbox), ADR-0014 (accounts)

## Context

The workspace only worked in one direction. An agent could write files into
`/workspace` and they came back as artifacts, but a reader who wanted a
spreadsheet analysed could not put it there: `POST /api/agents/run` answered
`422 File uploads require Tier 2` to any request carrying `fileIds`. The
composer had a full upload widget pointing at `/api/files/upload`, an endpoint
that has never existed in this codebase, and the button was gated on the
model's *native* file API — which DeepSeek does not have — so it never even
appeared.

Two properties made this harder than "accept a multipart body":

* A conversation runs in one of two workspaces that live in different places:
  a host directory (`AGENTFORGE_WORKSPACE/<session>`) for a plain run, a bind
  mount into a Docker container (`sandboxes/<id>`) for an isolated one. The
  model only ever sees the active one.
* A sandbox is destroyed when its lease is released, so anything stored only
  inside it cannot be downloaded afterwards.

## Decision

**Store the bytes outside any workspace; copy them in per turn.**

Blobs live under `AGENTFORGE_WORKSPACE/_uploads/<session>/<random id>`, with
one metadata row per file (`session_files`: name, label, size, sha256, mime,
`sessionId`, `owner_id`). At the start of each run the session's blobs are
copied into `<workspace>/uploads/<label>` — whichever workspace that run uses —
and the file list is appended to the system prompt, because a model that is not
told about a file will answer "I cannot see uploaded files" while the file sits
one `read_file` away.

**The copy is one-way.** The workspace copy is a per-turn snapshot; edits the
agent makes to it stay in that turn. The reader's file is the blob, so a
mistaken `rm` inside the container cannot destroy what they uploaded.

**Files belong to the conversation, not the message.** One upload is available
to every later turn in that conversation, survives a failed turn, a model
switch and a page reload. Attaching a file therefore also selects agent mode:
the plain completion path has no tools, and uploading into a mode that would
silently ignore the file is worse than not offering it.

**Trust nothing about the upload.**

| Risk | Bound |
|---|---|
| Client filename is a path | The name only ever becomes a display string and a sanitised `uploads/<label>`; the blob is named from `os.urandom`, created `O_CREAT\|O_EXCL\|O_NOFOLLOW`, mode 0600 |
| Client picks the type | Extension allowlist (documents, data, images, `.zip`); SVG and executables are absent by construction |
| A `.png` that is really a program | Leading bytes checked against the extension for PDF, images, zip and the zip-based Office formats |
| A body that lies about its length | Read in 64 KB chunks against a byte budget; the whole request is additionally refused in middleware from `Content-Length` before Starlette spools it |
| One file eats the disk | 25 MB per file, 20 files per conversation, 200 MB per account, all configurable |
| Symlink planted by the agent | The destination is unlinked before the copy, never followed |
| Another account's file | Rows are fetched inside the caller's owner scope; a foreign id is the same 404 as an unknown one |
| A file used in another conversation | `sessionId` must match the request; otherwise 404 |
| Upload as an anonymous user | Guests are refused outright — a guest owner id is free to mint, so a fillable disk is a denial of service |
| Active content rendered by the browser | Every download is `Content-Disposition: attachment` with `nosniff`; HTML additionally gets a sandbox CSP |
| A session id that escapes the store | `^[A-Za-z0-9_-]{1,64}$` at the door, because the id becomes a directory name |

Prompt injection through file content is accepted, not solved: the manifest
tells the model that file contents are data to analyse rather than instructions
to follow, which is the same posture the agent already takes toward tool output.

## Consequences

* Deleting a conversation deletes its blobs and rows; nothing is shared between
  conversations, so the same file uploaded twice is stored twice.
* The per-turn copy costs a full read and write of the session's files on every
  turn. Bounded by the 200 MB account quota, which is the real ceiling.
* The quota check is read-then-write, so two simultaneous uploads can each pass
  the last few bytes of the allowance. The overshoot is one file.
* A shadowed file name is renamed (`report-2.csv`) rather than refused, so the
  agent's view stays flat and predictable.
* Uploads need a reverse-proxy body limit that agrees with `UPLOAD_MAX_BYTES`;
  nginx's default 1 MB would reject anything larger with its own error page.
