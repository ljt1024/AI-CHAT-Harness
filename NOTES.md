# AgentForge implementation notes

## Decisions
- Task book v3 takes precedence over older sections of the development plan.
- Import the pinned base locally; retain this repository's origin and document history.
- Tier 1 uses one runtime process; MongoDB is the production persistence backend.
- Restricted host tools default to a dedicated workspace; Docker isolation arrives in T10.

## Backlog
- T10-T12: Docker isolation, warm pool, circuit breaker, GridFS snapshots.
- T13-T14: Complete platform endpoints and frontend wizard/approvals/trace/Inspector.
- T15-T20: Skills, delegation, benchmark suite, hardening, full-stack deployment.

## Verification
- See task commits and harness/tests for executable acceptance coverage.
- Docker Desktop starts, but `docker compose up -d mongo` fails against configured registries (EOF, invalid manifest/referrers index). T0 infrastructure acceptance is pending; mock-backed core tests do not replace this check.
- Core implementation and isolated tests were prepared while infrastructure was unavailable; no full Tier 1 acceptance is claimed until MongoDB and browser checks are complete.
- ESLint adoption is scoped to migrated infrastructure and agent API TypeScript; legacy frontend TypeScript lint debt is deferred. The immutable end/ tree is excluded.
