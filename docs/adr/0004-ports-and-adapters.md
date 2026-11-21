# 0004 — Use narrow ports and adapters for external systems

- Status: Accepted
- Date: 2026-11-21
- Deciders: project maintainers
- Supersedes: —

## Context

Docker, gRPC, `subprocess`, Linux namespaces, and filesystem paths are used
directly across models, services, launchers, routing, and traffic. Tests
therefore patch concrete internals, and swapping a backend or running pure unit
tests is difficult.

## Decision

Introduce narrow protocols at the point of use for external boundaries:

- command execution;
- container runtime;
- GoNetEm gRPC operations;
- network namespace/veth operations;
- filesystem locations used by orchestration.

Production adapters wrap the real libraries. Tests provide fakes. Existing
constructors keep working by defaulting to production adapters. A protocol is
introduced only when there is at least one real consumer and a concrete test
fake.

## Consequences

- Domain and orchestration tests can run without Docker, root, or gRPC.
- Adapters must stay thin and free of business logic.
- Some currently public methods become delegating facades.
- Namespace behavior still needs live integration validation.

## Alternatives considered

- **Global monkeypatching of libraries:** rejected because it hides dependencies
  and remains difficult to test.
- **A single generic service locator:** rejected because it obscures dependency
  direction and encourages coupling.
