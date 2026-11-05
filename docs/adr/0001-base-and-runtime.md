# 0001: Preserve AI-CHAT and introduce a Python runtime
Background: Task book v3 supersedes the earlier Next.js and TypeScript gateway plan.
Decision: Import AI-CHAT 2.1.1 at fc78865fcea983503535f46f889886f33b28e872.
Decision: Keep React/Vite and the original end/ tree; implement new behavior in harness/.
Decision: Use FastAPI, motor/MongoDB, uv, and an in-process event bus for Tier 1.
Alternative: Rewrite the frontend or reuse LangGraph; rejected by the task book.
Consequence: Python implements both the native and legacy SSE contracts.
Consequence: Docker and real credentials remain explicit integration prerequisites.
