# 0006 — Services and TopologyManager

- Status: Proposed
- Date: 2026-11-21
- Depends on: 0001, 0002, 0003
- Supersedes: —

## Context

`TopologyManager` is 1,121 lines and composes nine mixins through broad shared
state. `topology_sync.py` (773 lines) mixes an 8-way link-key type switch,
satcom conversion, distance batching, and the tick pipeline. `LinkOpsMixin`
imports protobuf enums and optionally calls launcher methods. `NetworkConfig`
copies fields in three places. `diagnostics.py` and `controller.py` call
`subprocess` and hard-code paths. `Node` owns Docker and namespace operations;
`Link` bypasses the existing `CapacityStrategy` abstraction. `NodeTerminal` is
implemented twice.

## Requirements

- **MUST** document the mixin contract (required attributes, methods,
  initialization order, locks, events) and test it with a shared harness.
- **MUST** extract pure link normalization/diff planning from
  `topology_sync.py`; leave satcom I/O in the mixin.
- **MUST** separate the simulation-loop state machine from thread management and
  test ordering, failure propagation, and shutdown.
- **MUST** move controller/diagnostics process calls behind the command port,
  unify `NodeTerminal`, and remove hard-coded filesystem paths from pure logic.
- **MUST** introduce a `LinkBackend` protocol and a pure operation-stats
  accumulator; preserve current stats semantics.
- **MUST** make `NetworkConfig` field copying single-source and add a
  round-trip test.
- **MUST** move `Node` container/namespace implementation behind ports while
  keeping public methods as delegating facades (sub-phase 5b).
- **MUST** let `Link` accept the existing `CapacityStrategy` abstraction.
- **MUST NOT** change public lifecycle, locking, or routing behavior.

## Acceptance criteria

- Mixin contracts are documented and tested.
- `controller`, `diagnostics`, and `traffic_testing` reach 70% coverage;
  `topology_satcom` and mixins do not regress.
- `LinkOpsMixin` depends on `LinkBackend`, not protobuf or concrete launchers.
- Sync diffing and link-key normalization are independently testable.
- Lifecycle/simulation tests and `scripts/verify_connectivity.py` pass.
- `Node` no longer imports Docker or manipulates namespaces directly.

## Known behavior conflicts

These require explicit decisions before changing code:

- `controller_node_iface_pattern` is documented but ignored; the implementation
  hard-codes `eth(50000 + node_id)`.
- Failed link operations may still be counted as attempted in stats.
- Public docs and annotations disagree on `start_gonetem()` and `next_step()`
  return values (resolved as documentation/annotation fixes in Spec 0001).

## Non-goals

- Rewriting the mixin architecture wholesale.
- Changing lock semantics or tick ordering.

## Risks and mitigations

- **Threading regressions:** high risk. Mitigation: characterization tests and
  a live integration run for sub-phase 5b.
- **Wide blast radius of Node changes:** keep public methods as facades and
  migrate one operation at a time.

## Verification

```bash
uv run --frozen --extra dev pytest -q tests/services tests/models
uv run --frozen --extra dev pytest -q --cov=satgonetem/services --cov-fail-under=70
```
