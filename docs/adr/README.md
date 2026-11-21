# Architecture Decision Records

This directory records the decisions that shape the SOLID/TDD adoption work.
Each ADR is immutable once accepted; superseding decisions get a new ADR.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-adopt-solid-and-tdd.md) | Adopt SOLID principles and test-driven development | Accepted |
| [0002](0002-develop-integration-branch.md) | Use `develop` as the integration branch | Accepted |
| [0003](0003-behavior-preserving-refactors.md) | Refactors are behavior-preserving and characterization-first | Accepted |
| [0004](0004-ports-and-adapters.md) | Use narrow ports and adapters for external systems | Accepted |
| [0005](0005-public-api-facades.md) | Preserve public API paths with compatibility facades | Accepted |
| [0006](0006-blocking-quality-gates.md) | Make quality gates blocking and ratcheted | Accepted |

## Template

```markdown
# NNNN — Title

- Status: Proposed | Accepted | Superseded
- Date: YYYY-MM-DD
- Deciders: ...
- Supersedes: ...

## Context

## Decision

## Consequences

## Alternatives considered
```
