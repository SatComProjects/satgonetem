# Development Guide

SatGoNetem is a Python 3.10+ satellite network emulation tool. Source lives in
`satgonetem/`, tests in `tests/`, module documentation in `docs/`. Development
uses `uv`, `pytest`, and `ruff`, and targets the `develop` branch.

This guide is the working agreement for humans and AI agents. It applies to
every change, including refactors. Phased specifications live in
[docs/specs/](docs/specs/README.md); architecture decisions are recorded in
[docs/adr/](docs/adr/README.md).

## Non-negotiables

1. **Specification first.** Do not guess requirements. Restate the request,
   acceptance criteria, edge cases, and constraints before writing code. Ask
   when a requirement is ambiguous.
2. **Test-driven development.** Write a failing test before implementation.
   Verify it fails for the expected reason, then make the smallest change that
   passes. Refactor only with tests green.
3. **Small, incremental changes.** One concern per change. Do not mix behavior
   changes, refactors, formatting, and dependency updates.
4. **Preserve behavior unless explicitly asked to change it.** Refactors must
   be behavior-preserving and covered by tests before and after.
5. **Never weaken tests to get green.** Do not delete assertions, loosen
   tolerances, add skips, or over-mock to make a test pass. Fix the cause.
6. **Never claim a check passed without running it.** Report the exact command,
   exit status, and any relevant failure output.
7. **Report unverified assumptions.** If something could not be executed
   locally (Docker, root networking, external services), say so explicitly.
8. **Review the complete diff before declaring completion.** Include
   architecture tradeoffs and known limitations in the final report.

## Branch and pull-request policy

- `develop` is the integration branch. All work starts from an up-to-date
  `develop` and targets `develop` in a pull request.
- `main` is release-only. Do not commit feature work to `main`.
- Use short-lived branches:
  `feat/<topic>`, `fix/<topic>`, `refactor/<topic>`, `test/<topic>`,
  `docs/<topic>`, `chore/<topic>`.
- A pull request description states: the specification, the RED evidence
  (failing test and command), the implementation summary, validation commands
  and results, architectural tradeoffs, and unverified assumptions.
- Required CI checks (`Lint`, `Type check`, `Test` on 3.10–3.14, coverage at
  least 75%, and `Build package`) must pass before merge on `develop`. `main`
  adds `Type check` when the Phase 0 workflow is promoted. Do not bypass or
  disable a gate to land a change.

## Six-stage workflow

### 1. Understand

- Restate the requirement and acceptance criteria.
- List edge cases, error paths, and explicit non-goals.
- Identify affected packages, public APIs, configuration, and documentation.
- Name constraints: Python versions, Docker/network prerequisites, performance,
  backwards compatibility.

### 2. Design

- Identify responsibilities, interfaces, and dependency direction before
  coding.
- Check the change against the architecture rules below and SOLID
  responsibilities.
- Prefer the simplest design that satisfies the specification. Avoid
  speculative abstractions and new layers without a demonstrated need.
- For cross-module or public-API changes, discuss alternatives and the tradeoff
  before implementation.

### 3. Test (RED)

- Add the smallest tests that express the new behavior: unit tests for logic,
  integration tests where a real boundary is under test.
- Run the tests and confirm they fail for the expected reason (not an import
  error or a typo).
- A bug fix starts with a test that reproduces the bug.

### 4. Implement (GREEN)

- Write the smallest production change that makes the failing tests pass.
- Do not add unrelated refactors, renames, or formatting churn in the same
  change.
- Keep dependencies explicit and injected where a boundary is involved.

### 5. Refactor

- With tests green, remove duplication, clarify names, and simplify
  abstractions.
- Re-check SOLID responsibilities, especially SRP and DIP, without introducing
  indirection for its own sake.
- Refactoring commits must not change behavior. If behavior must change, go
  back to stage 3 and add a test.

### 6. Validate

- Run the narrowest relevant tests first, then the full suite.
- Run lint/static analysis. Run `uv lock --check` when dependencies change.
- Run dependency/security checks when dependencies or packaging change
  (`pip-audit`; see Spec 0008).
- Inspect the complete `git diff`, including generated or untracked files.
- Report: commands run, exit statuses, architectural tradeoffs, and anything
  unverified.

## Architecture rules

### Boundaries

- `satgonetem/models/` — domain data and pure behavior. No orchestration,
  Docker, subprocess, gRPC, or filesystem concerns. Existing exceptions (for
  example `models/node.py`) are tracked in `docs/specs/0006-services.md`; do not
  add new ones.
- `satgonetem/services/` — orchestration of topology lifecycle, interfaces,
  routing, traffic, and simulation. `TopologyManager` composes capabilities
  through the mixins in `satgonetem/services/mixins/`.
- `satgonetem/launchers/` — execution backends behind the
  `satgonetem/launchers/base_launcher.py` contract. New backends implement the
  contract; they do not add branches to callers.
- `satgonetem/routing/` — routing-daemon implementations behind
  `satgonetem/routing/base_daemon.py`. A new routing method is a new daemon,
  not an `if` branch in existing daemons.
- `satgonetem/traffic/` — traffic generation and result parsing. Keep command
  execution, output parsing, and domain results separable.
- `satgonetem/addressing/` and `satgonetem/link_budget/` — self-contained
  domain packages with their own documented APIs.
- `satgonetem/utils/` — generic helpers. Do not move domain logic here.
- `satgonetem/ports.py` — narrow protocols and default adapters for external
  processes, containers, and gRPC. Inject a fake or alternate adapter instead
  of calling the library directly.
- `satgonetem/proto/` — generated protobuf/gRPC code. Do not hand-edit; change
  the `.proto` source and regenerate.

Dependencies point inward/toward stable abstractions. Do not introduce import
cycles, and do not import services or launchers from models.

### SOLID, pragmatically

- **Single responsibility:** one reason to change per module/class. Split god
  classes by capability, not by line count.
- **Open/closed:** extend through existing base classes, strategies, and
  composition (launchers, routing daemons, link-budget strategies) instead of
  editing type switches in callers.
- **Liskov substitution:** subclasses and implementations honor the documented
  base contract, including error behavior and return types.
- **Interface segregation:** keep base/mixin surfaces narrow and
  capability-specific; do not force implementations to depend on methods they
  do not use.
- **Dependency inversion:** domain and orchestration code depend on
  abstractions for external systems (Docker, gRPC, `tc`/`ip`, network
  namespaces). Inject those boundaries so tests can substitute fakes.
- Apply the rule of three before extracting an abstraction. Do not add a layer,
  factory, or plugin system without a concrete second use case.

## Testing rules

- Tests mirror the package layout: `tests/<package>/test_<module>.py`.
- Prefer deterministic unit tests. Mock at external boundaries (Docker, gRPC,
  subprocess, network), not at internal domain logic.
- Use `tests/support/` for shared builders and fakes; do not add duplicate
  harness code.
- The default suite excludes `slow` and `benchmark` tests; run them explicitly
  with `-m "slow or benchmark"`.
- Integration tests that require Docker, root, or live networking must state
  the prerequisite and fail clearly when it is absent. Do not silently skip.
- Test behavior and contracts, not implementation details or call sequences
  unless the interaction is the contract.
- Every bug fix includes a regression test.
- Coverage is a diagnostic, not a target. Do not game it.

## Required commands

Run from the repository root with the locked environment:

```bash
uv sync --frozen --extra dev                                  # install
uv run --frozen --extra dev ruff check satgonetem tests       # lint
uv run --frozen --extra dev ruff format --check satgonetem tests  # format
uv run --frozen --extra dev mypy                              # full type check
uvx pip-audit@2.9.0 --path .venv/lib/python3.14/site-packages    # dependency audit
uv run --frozen --extra dev pytest -q                         # full suite
uv run --frozen --extra dev pytest tests/services/test_link_ops.py -q  # targeted
uv run --frozen --extra dev pytest -q --cov=satgonetem --cov-report=term-missing --cov-fail-under=75
uv lock --check                                               # dependencies changed
```

Notes:

- Ruff's `select` list is a deliberate ratchet (see `pyproject.toml`). Never
  narrow it; widen it as findings are fixed.
- `mypy` is enforced over the whole package. `attr-defined` remains disabled
  for generated protobuf and mixin-injected state (tracked in Spec 0008).
- Docker/network-dependent tests may not run in every environment. Report that
  explicitly instead of treating it as a pass.

## Definition of done

- [ ] Specification and acceptance criteria are stated.
- [ ] RED test was added and observed failing for the expected reason.
- [ ] Implementation is the smallest change that satisfies the tests.
- [ ] Tests remain green; no test was weakened or skipped to achieve it.
- [ ] Targeted tests and the full suite were run, with results reported.
- [ ] `ruff check satgonetem tests` passes; other static analysis run or
      explicitly reported as not run.
- [ ] Dependency/security checks run when dependencies changed, or explicitly
      reported as unavailable.
- [ ] Documentation is updated for public API, configuration, or behavior
      changes.
- [ ] The complete diff was reviewed; unrelated changes were removed.
- [ ] Architecture tradeoffs and unverified assumptions are reported.
