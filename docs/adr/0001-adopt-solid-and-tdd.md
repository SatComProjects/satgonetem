# 0001 — Adopt SOLID principles and test-driven development

- Status: Accepted
- Date: 2026-11-21
- Deciders: project maintainers
- Supersedes: —

## Context

The codebase is a research-oriented emulator with a working test suite, but
responsibilities are concentrated in large modules, external systems are called
directly from domain and orchestration code, and tests frequently patch internal
implementation details. Quality gates are mostly advisory. This makes changes
harder to review and increases regression risk in network-emulation behavior.

## Decision

Adopt SOLID as a pragmatic design guideline and test-driven development as the
default change workflow:

1. Specify behavior and acceptance criteria before implementation.
2. Write a failing test first, verify the failure reason, then implement the
   smallest passing change.
3. Refactor only with tests green and without changing behavior.
4. Preserve architectural boundaries and make dependencies explicit.
5. Run lint, static analysis, tests, and security checks before completion.

## Consequences

- Changes are smaller and more reviewable.
- Refactors require characterization tests, which costs time upfront.
- Some existing public behavior must be documented rather than silently
  "cleaned up".
- Quality gates become blocking; local workflows must run the same commands as
  CI.

## Alternatives considered

- **Rewrite the codebase:** rejected due to risk and loss of working emulation
  behavior.
- **Tests after implementation:** rejected because it reproduces the current
  blind spots.
- **Advisory lint/type checks:** rejected because unenforced gates do not
  prevent regressions.
