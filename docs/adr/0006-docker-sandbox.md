# 0006: Isolated execution and sandbox ownership
Background: Tier 1 host tools are restricted but do not provide a container boundary.
Decision: Docker SDK creates non-root, read-only containers with one assigned workspace mount.
Decision: Apply memory/CPU/PID caps, cap-drop ALL, no-new-privileges and an explicit seccomp allowlist.
Decision: Default network mode is none; bridge alone cannot enforce the plan's default-deny egress.
Decision: Pass an explicit environment only; never inherit Harness credentials or mount Docker sockets.
Decision: Timeout or cancellation destroys the container, terminating the entire process tree.
Alternative: Cancel a Docker SDK thread only; rejected because the in-container command would keep running.
Consequence: Each warm container has its own workspace; cached containers retain one session owner.
Consequence: Outbound network allowlists require a dedicated proxy and are not enabled by this change.
