# 0008 — Enforcement and final validation

- Status: Proposed
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
