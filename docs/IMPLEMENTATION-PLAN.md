# CLI MVP implementation record

The CLI requirements and reviewed chat design govern implementation. GUI is deferred.

## Architecture

One Python package (`src/dev_orchestra`) contains domain models, application services,
orchestration, agents and infrastructure. Typer is an adapter only. Future React lives
under `apps/web`; future FastAPI calls the same application services.

Host-owned commits, immutable PRD/config snapshots, SHA-bound validation and approvals,
test-only patch application, independent read-only review, bounded fixes and SQLite
checkpoints are the core invariants. No workflow push or merge commands are provided.

## Phases and gates

1. Bootstrap: isolated Python >=3.11, dependency lock, package smoke test.
2. Engine: strict config, SQLite ownership, worktree isolation, process timeouts,
   Codex capability checks and artifact persistence; unit tests.
3. Agents: typed outputs, test patch path restrictions, independent read-only review;
   protocol and malicious patch tests.
4. Orchestrator: fixed baseline, bounded fix loop, durable checkpoints, cancellation,
   evidence-bound approval; mocked workflow integration tests.
5. CLI: required eight commands plus operational helpers; Typer integration tests.
6. Verification: temporary Git repos, restart/failure/security scenarios, documentation,
   opt-in real Codex E2E only after user authorization for model usage.

Each completed phase is tested, committed and pushed. Real model calls are not part
of routine tests. Security guarantees must be distinguished from mocked verification.
