# Autonomous Docker Commands

## Decision

The default offline Docker sandbox now authorizes run_command through a rule rather than asking
for confirmation on every command. This includes installed skill scripts and file verification.
The permission manager recognizes the concrete SandboxTool type and rejects a stopped sandbox.
It does not infer safety by parsing shell text or automatically authorize tools solely by name.

This deliberately trusts the existing Docker boundary: non-root, read-only container root,
network disabled, no inherited credentials, only the managed session workspace writable,
resource caps and timeout destruction. Commands may change/delete their own workspace contents.
This does not grant host shell, Docker socket, extra mounts, network or external service access.

Host ShellTool, install_skill and MCP calls retain their mandatory confirmation. A saved agent
can explicitly raise run_command to confirm; existing max-permission semantics are preserved.
Docker failures never fall back to host execution. No broad remembered approval is added.

Runtime tool descriptions, loaded-skill responses and system instructions explain the same policy
so the model does not ask for routine sandbox permission in prose. This supersedes earlier ADR
statements that all sandbox shell commands require confirmation; file delivery is unchanged.

## Verification

Tests cover repeated Docker commands without pending approvals, real Docker-generated HTML
delivery, autonomous skill discovery/load/execution, explicit allow/deny policies, stopped sandbox
rejection, same-name non-sandbox tools and retained host/install/MCP approval behavior.
The browser regression accepts --sandbox-checks to require actual model-requested Python checks
without approval events, then validates preview, interactive download and reload persistence.
