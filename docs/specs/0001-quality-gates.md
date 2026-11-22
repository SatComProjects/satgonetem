# 0001 — Quality gates and baseline

- Status: Implemented
- Date: 2026-11-21
- Depends on: —
- Supersedes: —
- Amended: 2026-11-22 — engineering principles (see below)

## Context

The repository has a useful test suite and CI, but the gates are weak:

- ruff checks only `E9`, `F63`, `F7`, and `F82`;
- mypy, black, and flake8 are installed as development dependencies but are
  never configured or run;
- coverage is reported but not enforced;
- no pytest markers or strict marker validation exist;
- `setup.py` duplicates stale metadata that conflicts with `pyproject.toml`;
- the release workflow does not depend on lint or test results;
- branch protection does not require pull requests or status checks;
- several public docs disagree with the implementation.

The measured baseline at `9505aae` on `develop`: 862 passed, 1 skipped, 62%
coverage, 55.9 s; scoped mypy reports 16 errors and repo-wide mypy reports 293
errors; ruff passes with the four-rule selection.

## Requirements

- **MUST** declare `unit`, `integration`, `slow`, and `benchmark` pytest
  markers and run pytest with `--strict-markers`.
- **MUST** enforce a 62% global coverage floor in CI, omitting generated
  protobuf modules from coverage.
- **MUST** extend ruff to `E4`, `E7`, `E9`, `F401`, `F63`, `F7`, `F811`, `F82`,
  `F841`, and `I`, excluding generated protobuf sources.
- **MUST** add a blocking mypy job for `models`, `addressing`, and
  `link_budget`, and fix the current errors in those packages.
- **MUST** make `pyproject.toml` the single packaging metadata source and
  remove `pytest` from runtime dependencies.
- **MUST** make the release workflow run lint, type checking, and tests before
  building or publishing.
- **MUST** require pull requests and the CI status checks on `develop` and
  `main` through repository rulesets.
- **MUST** resolve documentation/code conflicts that do not require a behavior
  change, or record them as explicit follow-up issues.
- **MUST** record the baseline and update the specs/ADRs as gates land.

## Acceptance criteria

- `uv run --frozen --extra dev pytest -q` passes with `--strict-markers`.
- `uv run --frozen --extra dev ruff check satgonetem tests` passes with the
  expanded rule set.
- `uv run --frozen --extra dev mypy` passes for the scoped packages.
- CI jobs `Lint`, `Type check`, `Test (Python 3.10)` through
  `Test (Python 3.14)`, and `Build package` exist and pass.
- CI fails below 62% coverage.
- `uv lock --check` passes after dependency metadata changes.
- `uv build --out-dir dist` succeeds and the wheel smoke test imports the
  package.
- `release.yml` cannot publish without a successful verify stage.
- Rulesets for `develop` and `main` require a pull request and the named
  checks.
- No production behavior changes are required by this spec.

## Non-goals

- Raising coverage above the initial floor.
- Classifying the whole test suite into markers (Spec 0002).
- Full-repository type checking (Spec 0008).
- Refactoring production modules (Specs 0002–0007).

## Risks and mitigations

- **Import-order-sensitive code:** ruff import sorting can reorder imports
  around the deliberate `apply_satcom_fix()` call. Mitigation: keep the
  documented `E402` exception in `topology_satcom.py` and run the full suite
  after sorting.
- **Type-check scope creep:** fixing 293 repo-wide errors is out of scope.
  Mitigation: block only three domain packages and track the remainder.
- **Branch protection lockout:** enabling required checks before CI passes can
  block future work. Mitigation: push and verify the jobs first, then apply
  the rulesets.
- **Release failures:** a verify stage that installs the full development
  environment increases release time. Accepted for correctness.

## Verification

```bash
uv lock --check
uv run --frozen --extra dev ruff check satgonetem tests
uv run --frozen --extra dev mypy
uv run --frozen --extra dev pytest -q --cov=satgonetem --cov-fail-under=62
uv build --out-dir dist
```

Expected: all commands exit 0; CI shows all required checks.

## Implementation result (2026-11-21)

- `pytest -q --cov=satgonetem --cov-fail-under=62`: 862 passed, 1 skipped,
  63.98% coverage.
- `ruff check satgonetem tests`: passes with the expanded rule set.
- `mypy`: passes for the 27 scoped source files.
- `uv lock --check`, `uv build`, and `twine check`: pass.
- `setup.py` removed; `pyproject.toml` is the metadata source.
- CI now has `Lint`, `Type check`, `Test (Python 3.10–3.14)`, and
  `Build package`; coverage is enforced at 62%.
- Release workflow verifies lint, types, and tests before building.
- Repository rulesets require pull requests on `develop` and `main`.
  Both require `Lint`, `Type check`, `Test (Python 3.10–3.14)`,
  `Security audit`, and `Build package` (see Spec 0008).

## Amendment (2026-11-22): engineering principles

The quality gates below also enforce the engineering principles in
[AGENTS.md](../../AGENTS.md#engineering-principles).

### Requirements

- **MUST** review every change against KISS, DRY, SOLID, separation of
  concerns, and YAGNI in addition to the existing gates.
- **MUST** reject duplicated business logic, speculative abstractions, layer
  entanglement (CLI/API vs orchestration/domain vs I/O/data access), and
  unrequested features.
- **MUST** handle errors explicitly, validate boundary inputs, and never
  hardcode secrets.
- **MUST** report what changed, tests executed and results, known limitations,
  and documentation updates before completion.

### Acceptance criteria

- The pull request description states the principle check and any justified
  exception.
- The final diff review confirms no duplicated business logic, no new layer
  coupling, and no unrequested capability.
- Error paths and boundary validation are covered by tests where practical; no
  secret material is present.
- The completion report contains the four required items: what changed; tests
  and their results; known limitations; and documentation updates.
