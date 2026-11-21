# Specifications

This directory contains the implementation specifications for the SOLID/TDD
adoption programme. Each specification states requirements, acceptance
criteria, non-goals, and verification commands. Architecture decisions are
recorded in [../adr/](../adr/).

## Status

| Spec | Phase | Status |
|---|---|---|
| [0001 — Quality gates and baseline](0001-quality-gates.md) | 0 | Implemented |
| [0002 — Test architecture](0002-test-architecture.md) | 1 | Implemented |
| [0003 — External boundaries](0003-external-boundaries.md) | 2 | Implemented |
| [0004 — Routing subsystem](0004-routing.md) | 3 | Implemented |
| [0005 — Traffic subsystem](0005-traffic.md) | 4 | Implemented |
| [0006 — Services and TopologyManager](0006-services.md) | 5 | Implemented |
| [0007 — Launchers, builders, utils](0007-launchers-builders-utils.md) | 6 | Implemented |
| [0008 — Enforcement and final validation](0008-enforcement.md) | 7 | Proposed |
| [0009 — Fix Phase 1 characterization defects](0009-defect-fixes.md) | 1.1 | Implemented |

## Rules

- A specification is agreed before implementation starts.
- Acceptance criteria are observable and testable.
- Behavior changes require their own specification; refactor specs are
  behavior-preserving.
- A spec is marked `Implemented` only after its verification commands have been
  run and recorded.
- Use [the template](#template) for new specs and keep the index above current.

## Template

```markdown
# NNNN — Title

- Status: Proposed | Accepted | Implemented | Superseded
- Date: YYYY-MM-DD
- Depends on: ...
- Supersedes: ...

## Context
Problem, current behavior, and why this change is needed.

## Requirements
Normative requirements (MUST/SHOULD).

## Acceptance criteria
Observable checks, including exact commands where practical.

## Non-goals
What this spec explicitly does not change.

## Risks and mitigations

## Verification
Commands and expected results.
```
