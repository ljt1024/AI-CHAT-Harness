# 0008: Connect isolated tools and browser approvals
Background: Sandbox modules and approval endpoints exist but are not usable together in chat.
Decision: Explicit sandbox mode acquires a per-session lease and executes all selected tools in it.
Decision: File tools run through a trusted Python worker in the container, not on the host.
Decision: Keep environment selection fixed for an existing session and fail closed on Docker errors.
Decision: Browser approval cards submit one-shot decisions through the infrastructure API.
Consequence: This integration supports the Python image; Node-only file workers remain deferred.
