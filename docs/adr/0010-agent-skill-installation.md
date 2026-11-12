# 0010: Give agents an approval-gated skill installer

Status: Accepted

Context: Agents could discover and load skills but could not install them. Upload-only
administration did not satisfy conversational installation requests.

Decision: Register install_skill in all runtime registries alongside list_skills/load_skill.
The tool has a mandatory confirm permission that agent settings cannot lower. Accept public
GitHub folders, container workspace directories and explicit inline packages. Validate the
complete package before persisting, preserving content-addressed IDs and bounded UTF-8 files.

GitHub downloads are limited to fixed api.github.com endpoints with no redirect following,
ambient proxy use, embedded credentials or private repository authentication. Resolve a
single commit before reading folders/blobs. Bound network bytes, file count and duration.
Workspace sources use a bounded Docker archive and reject traversal, links and special files;
there is no arbitrary host path read. Never extract an untrusted archive to the host.

Consequence: Approval precedes download and persistence. Imported code is never executed.
Dependency installation remains separate and unsupported automatically. Emit skill_installed
only after persistence and refresh the frontend catalog. Failed/denied installs do not emit
success or leave partial packages. Actual script execution still has its own approval.

Existing conversations can contain outdated assistant refusals from before installation was
available. Each model call therefore receives an authoritative, transient tool manifest after
conversation history, derived from the actual enabled schemas. It is not appended to stored
history. Installation instructions distinguish backend GitHub downloads from the offline
sandbox. Verify natural requests against stale history, not only fresh conversations with
explicit tool names (scripts/verify_skill_history.py copies history without altering its source).

2026-10 update: real-world skills (e.g. nextlevelbuilder/ui-ux-pro-max-skill) ship data files
of hundreds of KB, so the original 32 KB per-file / 256 KB package bounds blocked them.
Bounds are now 1 MB per file, 24 KB for SKILL.md and 5 MB per package (still 100 UTF-8 text
files, still inside the 16 MB MongoDB document limit), and GitHub/workspace transport caps were
raised accordingly. Skill files are synced into the sandbox by writing the bind-mounted host
workspace directory directly (with symlink denial) instead of an in-container exec worker,
because exec argv is capped at 128 KB per argument on Linux and would reject larger files.

2026-10 (rate limits): importing that skill through the contents/blob endpoints cost ~84 API
calls, more than the unauthenticated 60/hour budget, so installs failed mid-way with
"GitHub access denied or rate limited" and retries kept the budget exhausted. File downloads
now go, in order, to raw.githubusercontent.com (no API cost), then a single codeload.github.com
commit archive (also no per-file API cost), with api.github.com blobs only as a last resort;
only the commit lookup and one recursive tree request hit the API. `GITHUB_TOKEN` /
`AGENTFORGE_GITHUB_TOKEN` is sent when set. All three hosts are GitHub-owned and pinned to the
resolved commit; no ambient proxy is used and redirects are still not followed.

2026-10 (deletion): installation had no counterpart, so removing a skill meant editing MongoDB
by hand while stale copies stayed in `/workspace/.skills/<id>`. `delete_skill` is now a normal
tool with `confirm` permission, matching installation. It requires an exact `<name>-<12 hex>`
identity (never a path), refuses to delete a skill still selected by a saved agent, unloads it
from the running agent, and removes only `.skills/<id>` from the sandbox workspace, refusing a
symlinked skill directory. The REST `DELETE /api/skills/{id}` uses the same manager method, now
answering 404 for unknown IDs instead of a silent 200, and also purges synced copies from every
managed workspace. Deletion is irreversible and re-installing is the only restore path, so the
panel asks for confirmation before calling it.
