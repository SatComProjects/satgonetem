# 0008 — Enforcement and final validation

- Status: Implemented
- Date: 2026-11-21
- Depends on: 0001–0007
- Supersedes: —

## Context

Phase 0 introduces scoped gates and temporary exceptions. The final phase must
make the gates comprehensive, remove tolerances that are no longer needed, and
verify the architecture against the working agreement.

## Requirements

- **MUST** widen mypy to the full `satgonetem` package and make it blocking.
- **MUST** widen ruff to `I`, `B`, `UP`, `SIM`, `E4`, `E7`, and formatting
  checks; remove unused formatter/linter dependencies.
- **MUST** raise global coverage to at least 75% and add a changed-line
  coverage check if tooling permits.
- **MUST** add dependency/security scanning and action linting; SHA-pin
  actions if accepted.
- **MUST** verify that `develop` and `main` require pull requests and all
  status checks, and that releases depend on green CI.
- **MUST** review dependency direction, import cycles, boundary violations,
  public facades, and generated-code handling against AGENTS.md.
- **MUST** record final metrics and remaining debt.

## Acceptance criteria

- `uv run mypy` covers the whole package and passes.
- `ruff check` and formatting checks pass with the full selected rules.
- Global coverage is at least 75%; no target module is below its phase floor.
- Security/dependency and action-linting jobs pass.
- Branch rulesets and release gating are verified.
- A final architecture review is recorded in the closing PR.

## Non-goals

- New features during enforcement.
- Strict mypy mode if the package is not clean at the current level.

## Risks and mitigations

- **Large type-check burn-down:** widen package by package and track the error
  count; never claim full typing before the job covers it.
- **Coverage gaming:** prefer contract/behavior tests and diff coverage.
- **Gate fatigue:** only add checks that are actionable and documented.

## Verification

```bash
uv run --frozen --extra dev ruff check satgonetem tests
uv run --frozen --extra dev ruff format --check satgonetem tests
uv run --frozen --extra dev mypy
uv run --frozen --extra dev pytest -q --cov=satgonetem --cov-fail-under=75
```

## Implementation result (2026-11-21)

- Ruff now enforces `B`, `E4`, `E7`, `E9`, `F401`, `F63`, `F7`, `F811`, `F82`,
  `F841`, `I`, `SIM`, and `UP`; `ruff format --check` is a CI gate.
- Full-package mypy is blocking. `attr-defined` remains disabled because
  generated protobuf attributes and mixin-injected state are not statically
  visible; the rest of the package is clean.
- Coverage floor raised to 75% globally; current coverage is ~80%.
- Security job runs `pip-audit`, high-severity `bandit`, and `actionlint`.
- `develop` and `main` require `Lint`, `Type check`, tests, `Security audit`,
  and `Build package`.
- `black`, `flake8`, and their transitive tooling were removed from dev
  dependencies; ruff is the single formatter/linter.
- Final suite: 1224 passed, 1 skipped, 9 deselected, 79.99% coverage; live
  connectivity check passes.
- Final architecture review: no runtime import cycles; models do not import
  services or launchers; generated protobuf remains excluded from lint.
