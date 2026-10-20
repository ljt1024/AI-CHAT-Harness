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
- 2026-10-20: Resolved image downloads using public.ecr.aws/docker/library/{python:3.12-slim,mongo:7}, then tagged locally. MongoDB compose health and real index creation pass. Earlier registry failures below are historical.
- T10 Docker runner passes real read-only root, writable workspace, PID cap, denied egress, zero inherited credential variables and timeout destruction checks.
- T11 pool passes real prewarmed acquisition under 500ms, cached reuse, idle eviction and workspace-preserving rebuild. This is warm acquisition, not a claim that cold creation is under 500ms.
- T12 snapshot module stores tar in real GridFS and validates ownership, integrity and archive paths on restore. File upload/download endpoints and runtime sandbox integration remain outstanding.
- See task commits and harness/tests for executable acceptance coverage.
- Docker Desktop starts, but `docker compose up -d mongo` fails against configured registries (EOF, invalid manifest/referrers index). T0 infrastructure acceptance is pending; mock-backed core tests do not replace this check.
- Core implementation and isolated tests were prepared while infrastructure was unavailable; no full Tier 1 acceptance is claimed until MongoDB and browser checks are complete.
- ESLint adoption is scoped to migrated infrastructure and agent API TypeScript; legacy frontend TypeScript lint debt is deferred. The immutable end/ tree is excluded.
- Browser transport smoke passed on desktop 1440x1000 and mobile 390x844 using the explicitly opt-in protocol stub and installed Chrome. Screenshots: harness/out/browser/.
- Production API integration covers create agent, run, approval/denial, persisted trace and usage, using mock MongoDB and deterministic adapters. Real model and Docker integration remain pending.
