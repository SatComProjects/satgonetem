# 0003 — External boundaries

- Status: Proposed
- Date: 2026-11-21
- Depends on: 0001, 0002
- Supersedes: —

## Context

Domain and orchestration code call external systems directly:

- `models/node.py` imports Docker, reads `/proc/*/ns/net`, calls `setns`, and
  creates veth/dummy interfaces;
- `launchers/gonetem_launcher.py` calls gRPC and Docker;
- `launchers/hil_manager.py` calls `nsenter`, `tc`, and `ip`;
- `services/mixins/controller.py` and `diagnostics.py` call `subprocess`;
- `traffic/iperf3_utils.py` and `iperf2_utils.py` perform OS integration and
  file I/O;
- routing and diagnostics use hard-coded `/tmp` paths and CWD-relative files.

This forces tests to patch internals and prevents substitution of a fake
backend.

## Requirements

- **MUST** define narrow protocols where they are consumed, or in a small
  package-local `ports.py`, for:
  - command execution;
  - container runtime access;
  - GoNetEm gRPC operations;
  - Linux network-namespace operations;
  - filesystem paths used by orchestration.
- **MUST** provide production adapters and test fakes for each protocol.
- **MUST** keep existing constructors working with default adapters.
- **MUST** keep `Node.execute_command` as a public delegating facade.
- **MUST** migrate consumers incrementally: routing execution, traffic
  execution, controller/diagnostics, launcher gRPC, HIL commands, Node
  container/namespace operations.
- **MUST NOT** add a protocol that has only one implementation and no concrete
  test fake.
- **MUST NOT** change generated commands, routing semantics, or public imports.

## Acceptance criteria

- Migrated orchestration/domain classes do not import `subprocess`, `docker`,
  or gRPC stubs directly.
- A fake implementation can substitute each protocol in tests and produce the
  same observable behavior.
- File paths used by orchestration are explicit and testable with `tmp_path`.
- `scripts/verify_connectivity.py` still passes in an environment with Docker,
  root, and GoNetEm; otherwise the unverified status is reported.

## Non-goals

- Removing the `sat_com_trajectopy` compatibility patch (Spec 0007).
- Redesigning the routing, traffic, or launcher APIs.

## Risks and mitigations

- **Namespace operations are process-global:** fake tests cannot prove real
  veth behavior. Mitigation: isolate behind `NetworkNamespace`, test argument
  construction, and require a live integration run.
- **Over-abstraction:** too many ports increase indirection. Mitigation: only
  ports with a production adapter and a test fake; no speculative layers.

## Verification

```bash
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev ruff check satgonetem tests
uv run --frozen --extra dev mypy satgonetem/models satgonetem/addressing satgonetem/link_budget
```
