# Specifications

This directory contains the implementation specifications for the
engineering principles (KISS, DRY, SOLID, separation of concerns, YAGNI) and
TDD adoption programme. Each specification states requirements, acceptance
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
| [0008 — Enforcement and final validation](0008-enforcement.md) | 7 | Implemented |
| [0009 — Fix Phase 1 characterization defects](0009-defect-fixes.md) | 1.1 | Implemented |
| [0010 — Wave 1 experiment platform and link controls](0010-wave1-experiment-platform.md) | 8 | Implemented |
| [0011 — Optional dual-stack IPv6 addressing](0011-ipv6-addressing.md) | 8 | Implemented |
| [0012 — Live integration test tier](0012-live-integration-tier.md) | 9 | Implemented |
| [0013 — Decompose the four largest classes](0013-god-class-decomposition.md) | 9 | Implemented |

Dated amendment sections are normative. They extend an implemented spec without
rewriting its original implementation record.

## Rules

- A specification is agreed before implementation starts.
- Acceptance criteria are observable and testable.
- Behavior changes require their own specification; refactor specs are
  behavior-preserving.
- Every spec MUST apply the engineering principles in
  [AGENTS.md](../../AGENTS.md#engineering-principles) and state its non-goals.
  YAGNI means unrequested capabilities are explicitly deferred, not implemented.
- Requirements MUST include explicit error handling and boundary input
  validation, and MUST NOT introduce hardcoded secrets.
- Acceptance criteria MUST include a principle check: no duplicated business
  logic, preserved layer boundaries (CLI/API, orchestration/domain, I/O/data
  access), and no speculative abstraction.
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

## Engineering principles
How the change applies KISS, DRY, SOLID, separation of concerns, and YAGNI.
State explicitly what is deferred (YAGNI).

## Requirements
Normative requirements (MUST/SHOULD), including error handling and boundary
input validation.

## Acceptance criteria
Observable checks, including exact commands where practical, plus the
principle-compliance check.

## Non-goals
What this spec explicitly does not change.

## Risks and mitigations

## Verification
Commands and expected results.

## Completion report
What changed, tests executed and results, known limitations, and documentation
updates.
```
