# 0002 — Test architecture

- Status: Implemented
- Date: 2026-11-21
- Depends on: 0001
- Supersedes: —

## Context

Tests are spread across 65 files and 807 functions with no shared
`conftest.py`. Helpers such as `_make_node`, `DummyTopology`, fake label
managers, and recording nodes are duplicated. Slow and benchmark tests are not
marked and run in every CI matrix leg. Several tests depend on the repository
root as the working directory or use real sleeps. Some assertions target
private attributes or exact internal call sequences, and one test file patches
a non-existent `ospf_daemon.open` target.

## Requirements

- **MUST** provide shared test builders/fakes in `tests/conftest.py` or
  `tests/support/` for nodes, links, topologies, command execution, gRPC, and
  Docker boundaries.
- **MUST** classify tests with `unit`, `integration`, `slow`, and `benchmark`
  markers; the default local/CI selection excludes `slow` and `benchmark`.
- **MUST** make tests independent of the current working directory and
  deterministic (events/conditions instead of sleeps where practical).
- **MUST** characterize current behavior before modifying production modules,
  starting with `FlowScheduler`, `p4_daemon`, `node_terminal_bridge`,
  `graph_shared_memory`, `interface_mgr`, `link_ops`, `topology_sync`,
  `traffic_testing`, `controller`, `diagnostics`, `bird_service`, `ip_utils`,
  `utils/coverage`, and the low-coverage SR-MPLS/ISIS paths.
- **MUST** remove dead patches, tautological assertions, and unjustified
  private-attribute assertions.
- **MUST** raise the global coverage floor to at least 65% at phase exit.
- **MUST NOT** change production behavior as part of this phase.

## Acceptance criteria

- New tests use the shared support layer; no new duplicate helpers are added.
- `pytest -q` passes from any working directory.
- No unmarked slow or benchmark test runs in the default suite.
- Coverage is at least 65%; every module targeted by Specs 0003–0007 has
  characterization tests before its refactor starts.
- Tests whose behavior depends on Docker, root, or live networking are marked
  and fail with a clear prerequisite message rather than silently skipping.

## Non-goals

- Fixing product bugs discovered by characterization tests. File a separate
  behavior spec instead.
- Achieving a specific test count or coverage target beyond the floor.

## Risks and mitigations

- **Characterizing bugs:** tests may lock in incorrect behavior. Mitigation:
  characterize documented contracts and observable behavior only; flag defects
  separately.
- **Marker drift:** `--strict-markers` prevents undeclared markers. Mitigation:
  document marker meanings in `AGENTS.md` and the test support README.

## Verification

```bash
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev pytest -q -m "slow or benchmark"
uv run --frozen --extra dev pytest -q --cov=satgonetem --cov-fail-under=65
```

## Implementation result (2026-11-21)

- Added `tests/conftest.py` and `tests/support/` with node/link/interface
  factories, dict-backed topology, command/container/gRPC fakes, and a
  deterministic `RecordingFlow`.
- Added 266 characterization/contract tests across services, routing, traffic,
  and utils: controller command construction, diagnostics helpers, traffic
  fan-out, interface manager, P4 controller lifecycle, shared-memory graph,
  terminal bridge helpers, FlowScheduler lifecycle/error/timeout behavior, IP
  utilities, coverage geometry, TLE/checksum/distance helpers, and geodesy.
- Default suite: 1126 passed, 1 skipped, 9 deselected, 1 xfailed, 71.36%
  coverage in 24.5 s. Extended set (`-m "slow or benchmark"`): 9 passed.
- Added `unit`, `integration`, `slow`, and `benchmark` markers with
  `--strict-markers`; the benchmark module and the sleep-heavy simulation-lock
  regression module are marked, and the default selection excludes both.
- Removed CWD-relative topology paths from the test suite, dead
  `ospf_daemon.open` patches, and the tautological link-budget assertion.
- Coverage floor raised from 62% to 65%; CI has a separate manual
  `Extended tests` job for slow/benchmark tests.
- Characterization exposed real defects; the first batch was fixed in
  [Spec 0009](0009-defect-fixes.md). Remaining behavior questions stay in the
  later subsystem specs.
