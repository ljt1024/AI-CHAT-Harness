# 0004: Middleware and permission enforcement
Background: Runtime extensions must remain independently switchable and permissions must follow impact.
Decision: Seven registered middleware layers use forward entry and reverse result/exit hooks.
Decision: Sandbox health/breaker and skill synchronization require adapters and default to disabled.
Decision: Read-only tools execute automatically; workspace writes use path rules; commands require approval.
Decision: Agent policies may escalate tool permissions but cannot lower the tool's minimum level.
Decision: Approval IDs are runtime-generated, one-shot and expire to denial; server binds to loopback.
Alternative: Trust model-provided IDs or in-band text approvals; rejected as ambiguous.
Consequence: Web approval controls and durable approval recovery remain Tier 2 work.
