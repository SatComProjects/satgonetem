# 0003 — Refactors are behavior-preserving and characterization-first

- Status: Accepted
- Date: 2026-11-21
- Deciders: project maintainers
- Supersedes: —

## Context

The refactoring programme moves code across modules, introduces ports, and
splits large classes. The emulator's observable behavior includes generated
`tc`/`ip` commands, routing decisions, YAML project files, timing, and public
Python APIs. Unit tests alone cannot prove that a structural change preserves
all of it.

## Decision

Every refactor follows this order:

1. Characterize the current observable behavior with tests before moving code.
2. Make the smallest structural change that keeps those tests green.
3. Separate mechanical moves from logic changes.
4. If a defect or an undocumented behavior is discovered, stop and write a
   separate behavior specification; do not fix it inside the refactor.
5. Validate with targeted tests, the full suite, lint/type checks, and an
   integration script where the change touches live networking.

## Consequences

- Refactor pull requests may include tests that were written earlier against
  the legacy behavior.
- Some refactors will be blocked until a behavior decision is made; this is
  intentional.
- Command/YAML fixtures become part of the compatibility contract.

## Alternatives considered

- **Refactor and fix defects in one change:** rejected because it makes
  regressions and review intent ambiguous.
- **Rely on the existing suite only:** rejected because several high-risk
  modules have little or no coverage.
