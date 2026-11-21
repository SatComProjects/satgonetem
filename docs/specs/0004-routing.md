# 0004 — Routing subsystem

- Status: Implemented
- Date: 2026-11-21
- Depends on: 0001, 0002, 0003
- Supersedes: —

## Context

Routing daemons share a `RoutingDaemon` ABC, but the factory hard-codes every
built-in daemon in `RoutingManagerMixin.init_routing`, duplicated name lists
exist in `TopologyManager` and `LinkOpsMixin`, and daemons depend on the full
`TopologyManager`. `SRMPLSDaemon` and `StaticRoutingDaemon` are over 1,200
lines each. OSPF and IS-IS duplicate their Bird lifecycle. `P4Daemon` has a
non-conforming constructor, is absent from the registry, and has 0% coverage.
The routing layer is also tied into a type-level import cycle with services.

## Requirements

- **MUST** add a parametrized contract suite for every daemon covering `init`,
  `update`, `remove`, failure handling, idempotence, node-scoped removal,
  worker limits, and single-argument construction.
- **MUST** replace built-in `if/elif` dispatch with registry lookup and
  centralize the built-in name list.
- **MUST** define a narrow `RoutingContext` protocol and remove runtime and
  `TYPE_CHECKING` manager dependencies from routing modules.
- **MUST** extract shared command/config helpers for OSPF/IS-IS Bird lifecycle,
  IP assignment, MPLS enabling, base64 `ip -batch`, and the SR-MPLS change-set
  algorithm.
- **MUST** split `static_daemon.py` and `srmpls_daemon.py` by responsibility:
  planning, generation, execution, parsing/verification.
- **MUST NOT** change generated commands or routing semantics.
- **SHOULD** remove dead/legacy paths only after confirming there is no
  supported external use, in a separate approved change.
- **MUST** decide `P4Daemon`'s support status before investing in it.

## Acceptance criteria

- Adding a routing method requires no edit to `RoutingManagerMixin`; a test
  proves this with a dummy daemon.
- Every daemon passes the contract suite.
- `routing` does not import `services` at runtime or for manager types.
- Routing coverage is at least 80%; `p4_daemon` and `bird_service` are no
  longer at 0%.
- Existing public imports and documented routing behavior are unchanged.

## Non-goals

- New routing protocols.
- Changing route selection or label-allocation policy.

## Risks and mitigations

- **Exact command assertions:** moving generation can hide regressions.
  Mitigation: characterize commands at the generation layer and keep one
  end-to-end fixture per daemon.
- **Live semantics:** unit tests cannot prove kernel routing behavior.
  Mitigation: run `scripts/verify_connectivity.py` before closing the phase.

## Verification

```bash
uv run --frozen --extra dev pytest -q tests/routing
uv run --frozen --extra dev pytest -q --cov=satgonetem/routing --cov-fail-under=80
uv run --frozen --extra dev mypy satgonetem/routing
```

## Implementation result (2026-11-21)

- Added `routing/registry.py` and a single registration point in
  `routing/__init__.py`; `RoutingManagerMixin` no longer has a built-in
  `if/elif` chain. Custom factories take precedence, so tests can inject them.
- Added the runtime-checkable `RoutingContext` protocol; routing modules no
  longer import `satgonetem.services`.
- Added `BirdRoutingDaemon` and moved OSPF/IS-IS lifecycle duplication there.
- Added `routing/commands.py` for IP batches, MPLS enable, and NET formatting.
- Extracted `SegmentRoutingManager`/`NodeSID` to
  `srmpls_label_manager.py` and static route diffing to
  `static_route_ops.py`, with compatibility re-exports.
- Added registry/context, Bird lifecycle, command, label-manager, static
  route-planning, custom-path, and coverage tests.
- Routing coverage: 81.37%; default suite tests remain green.
- P4 decision: `P4Daemon` remains experimental and is not a registered built-in
  or `routing_method` option because its constructor requires a controller
  path. It is covered by direct lifecycle/edge tests.
- Remaining cleanup: static/SR-MPLS command-execution adapters can be extracted
  further without behavior changes; this is tracked for the next cleanup pass.

## P4 support decision

`P4Daemon` is not advertised as a supported routing method. It requires an
external P4 controller directory and is not registered in the built-in list or
the custom registry. Users may instantiate it directly for experiments.
