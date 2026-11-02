# CLI MVP verification — 2026-11-02

## Phase results

| Phase | Scope | Result at phase checkpoint |
|---|---|---|
| 1 | Python/monorepo/bootstrap/lock | 1 test passed; project runtime Python 3.12.16 |
| 2 | Config, SQLite, Git, processes, Codex adapter | 9 tests passed |
| 3 | Role agents, guarded patches, JUnit and sandbox runner | 14 tests passed |
| 4 | Workflow, bounded fixes, checkpoint resume, approval | 20 tests passed |
| 5 | CLI and service adapter | 22 tests passed |
| 6 | Expanded failure/security checks, real E2E, packaging | 38 tests passed including actual macOS sandbox test |

Final commands:

- `DEVORCHESTRA_TEST_SANDBOX=1 .venv/bin/python -m pytest -q`: **38 passed**.
- `.venv/bin/ruff check src tests scripts`: passed.
- `.venv/bin/ruff format --check src tests scripts`: passed, 24 files.
- `.tools/uv build --offline`: wheel and source distribution built.
- `devorchestra doctor`: Codex exec capability and native macOS sandbox checks passed.

## Actual model E2E

User authorized real Codex usage. A temporary repository with a minimal calculator PRD
was used. Planning, development, test generation and independent review used actual
Codex CLI 0.165.0 sessions. Test execution used the native macOS sandbox and actual pytest.

Observed final evidence:

- State `completed` after SHA-bound human-approval operation.
- 9 pytest cases, 0 failures, 0 skipped, exit code 0.
- Review returned 0 findings.
- One approval recorded for the verified commit.
- Original repository checkout remained unchanged.

Initial failures exposed the `/var` canonical-path alias issue and malformed model diff
headers/hunk counts. Both were fixed; completed planning was preserved and the run resumed.
Diff normalization only adjusts section-header indentation and lets Git recount hunk sizes;
path validation, Git's own numstat interpretation and apply checks remain enforced.

A full real-model fix loop was not forced. Bounded fix loops, test failure, review failure,
budget exhaustion and resume scenarios were verified using mock agents and temporary Git
repositories. Native Linux/bwrap and non-Python project toolchains were not exercised.
See README safety limits; passing tests do not establish complete hostile-code isolation.

## Git publication

Phase commits are preserved locally. HTTPS Push attempts failed with repository write
permission denial, then invalid credentials. Existing SSH authentication could not be used
because the GitHub host key was absent; host verification was not bypassed. Publication
remains pending valid local GitHub authentication. No credentials are present in source,
configuration examples, test fixtures or documentation.
