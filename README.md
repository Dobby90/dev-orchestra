# DevOrchestra

Local Python CLI that orchestrates planning, development, test authoring/execution,
independent review and bounded fixes using Codex CLI. Human approval records the
verified commit; it never pushes or merges the target project.

The repository is a monorepo: the shared Python backend lives in `src/dev_orchestra`.
A future React app belongs in `apps/web`. No FastAPI or React code is implemented.

## Install

Requires Git, Python >=3.11, Codex CLI (developed against 0.165.0), and an OS sandbox.
macOS uses Seatbelt; Linux requires `bwrap`. Windows is not supported by this MVP.

```sh
# With uv installed:
uv python install 3.12
uv sync --frozen
uv run devorchestra --help
# Or with an existing Python >=3.11:
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
```

This workspace also has an ignored independent Python 3.12 runtime in `.python`,
`uv` in `.tools`, and a `.venv`. Reproduce the locked environment with:

```sh
UV_CACHE_DIR="$PWD/.uv-cache" .tools/uv sync --frozen
.venv/bin/python -m pytest -q
```

Authenticate Codex separately using `codex login`. Authentication and usage billing
follow the installed Codex configuration/account. Routine tests do not invoke models.

## Configure

Copy `config.example.yaml` to your own configuration file. Replace the test interpreter
and commands with those for your target project. Pass `--config` before the subcommand.
YAML paths are resolved relative to the config file. Default state is outside repositories
under `~/.devorchestra`; storage inside the target repository is rejected.

Test command IDs are an allowlist, not arbitrary model-provided shell strings. Commands
are executed as argument arrays without a shell. JUnit XML is mandatory so zero-test,
all-skipped, missing-report and malformed-report executions cannot pass. Java/Maven,
Gradle and JavaScript runners can be configured if they emit JUnit XML. DevOrchestra
does not install project dependencies or select commands without host configuration.

`write_paths` authorizes test patch locations. `output_paths` authorizes **untracked**
build/report directories only; a directory containing tracked source is rejected.
Dependencies must already be available offline. Maven/Gradle/npm commands can need
project-specific cache/output configuration; network access is blocked during tests.

## Commands

```sh
devorchestra --config config.yaml doctor
devorchestra --config config.yaml run --repo ~/projects/example --spec PRD.md --base main --max-fix-attempts 3
devorchestra --config config.yaml run --repo ~/projects/example --spec PRD.md --detach
devorchestra status RUN_ID
devorchestra list
devorchestra logs RUN_ID --follow --role reviewer
devorchestra pause RUN_ID
devorchestra resume RUN_ID
devorchestra resume RUN_ID --answers answers.md
devorchestra resume RUN_ID --max-fix-attempts 5
devorchestra cancel RUN_ID
devorchestra report RUN_ID
devorchestra approve RUN_ID
devorchestra cleanup RUN_ID
```

Use the same `--config` or `--database` on later commands when using nondefault storage.
`codex-pipeline` is an equivalent CLI alias. Query commands output JSON. `run`/`resume`
exit with code 2 for failure or required intervention; command/input errors return 1.
Waiting for final approval returns 0. Foreground Ctrl+C requests a safe pause; detached
workers continue after the CLI exits. Cancellation is acknowledged after subprocess
termination. Cleanup preserves branches, commits, reports and SQLite history; dirty
worktrees are never force-removed.

## Workflow and evidence

`queued → preparing → planning → developing → testing → reviewing`.
Failed tests or blocking review findings enter `fixing → testing → reviewing`, up to
three fixes by default. Successful validation produces a report and `waiting_approval`.
Ambiguous PRDs and exhausted fix budgets enter `needs_input`; process failures enter
`failed`. `resume` validates checkpoints and preserves completed stages. It never
resets the fix budget. `approve` requires unchanged clean HEAD and untampered evidence.

All agents run in fresh, read-only Codex sessions and return structured output. Code
changes use unified patches validated and applied by the host. Existing tests are
protected from developer/fixer edits. Deletions, renames, binary patches, symlinks and
permission changes require human handling. Test Agent patches are limited to configured
test paths. Planning acceptance criteria are immutable throughout each automatic loop.
Model-generated test coverage is a traceability assertion, not a mathematical proof.

Artifacts live under `runs/RUN_ID/iterations/ITERATION/ROLE/attempt-NNN/`. Each attempt
has a schema, response, logs, result and process marker. Latest summaries, PRD/config
snapshots and `final-report.md` live at the run root. State/event history is in SQLite
(WAL, foreign keys, short transactions). OS locks serialize execution per database and repository.
Final results and approvals bind to Git commit SHAs. Modified approved results are
reported as requiring intervention, while historical approvals remain in the audit log.

## Safety and current limits

- Original checkout edits and current branch are preserved. Only committed baseline
  code enters the worktree; local uncommitted changes are not copied.
- Feature branches/worktrees share Git metadata. Agents cannot write Git metadata;
  host Git commands disable hooks, signing and fsmonitor and never push/merge.
- Tests run offline with source read-only, explicitly writable output directories,
  an isolated temporary HOME and filtered environment. Known credential locations
  and project `.env`/key files are denied on macOS. This is not a complete hostile-code
  isolation boundary: arbitrary unknown secret files elsewhere may still be readable.
  Linux's bwrap backend needs independent deployment validation and has broader read access.
- Codex's read-only sandbox restricts writes; it does not guarantee all unrelated host
  files are unreadable. Use a dedicated account/container for untrusted repositories.
- PRD/repository text cannot choose host test commands or approve results. Prompt
  injection and semantically weakened tests cannot be completely detected automatically.
- Crash after commit but before checkpoint acknowledgement, externally modified HEAD,
  dirty worktrees, or a surviving orphan process require manual inspection. Data is
  preserved; DevOrchestra never resets/cleans it to claim successful recovery.
- A SHA may change after approval; approval is always historical evidence for its SHA.
  There is no filesystem watcher; `status` checks current approval validity.
- Logs are streamed and redacted before persistence, with an 8 MiB limit per process.
  Structured model responses and PRD snapshots may contain sensitive project content;
  keep state private. Redaction cannot identify every possible secret format.
- One worker runs at a time per SQLite database; repository locks also serialize
  operations when different databases target the same Git common directory.
- No GUI, API server, remote execution, dependency bootstrap for target projects,
  automatic push/merge or distributed queue is included.

## Development and verification

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
# Explicit model-usage consent is required before this command:
uv run python scripts/e2e.py
# Real OS sandbox tests, no model calls:
DEVORCHESTRA_TEST_SANDBOX=1 uv run pytest tests/test_security.py -q
```

The opt-in E2E creates a temporary Git repository, runs actual Codex roles and OS-sandboxed
pytest, approves a successful result and checks original checkout preservation. It prints
its temporary artifact location for inspection. Real model failures remain failures.
