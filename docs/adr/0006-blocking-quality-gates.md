# 0006 — Make quality gates blocking and ratcheted

- Status: Accepted
- Date: 2026-11-21
- Deciders: project maintainers
- Supersedes: —

## Context

Ruff, mypy, black, flake8, coverage, and branch protection exist partially or
not at all. The result is that CI can be green while type errors, dead imports,
or coverage regressions are present. Enabling every rule at once would create
an unreviewable cleanup.

## Decision

Adopt blocking, ratcheted gates:

- Start with the rule sets that can be made clean in Phase 0.
- Never remove a rule or lower a coverage threshold to get green.
- Add rules/packages in measured steps; track counts in the specification.
- Enforce the same checks locally and in CI.
- Require the checks on `develop` and `main`.
- Make release depend on a successful verify stage.

## Consequences

- CI becomes the source of truth; local commands must match it.
- Temporary per-file ignores are allowed only with a comment explaining why.
- Cleanup work is incremental rather than a formatting big bang.
- Repository settings become part of the delivery process.

## Alternatives considered

- **Enable all rules immediately:** rejected due to a large, risky diff.
- **Keep gates advisory:** rejected because unenforced rules do not prevent
  regressions.
- **Rely on pre-commit only:** rejected because it is bypassable and not a
  merge gate.
