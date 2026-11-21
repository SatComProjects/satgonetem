# 0009 — Fix Phase 1 characterization defects

- Status: Implemented
- Date: 2026-11-21
- Depends on: 0002
- Supersedes: —

## Context

Phase 1 characterized current behavior and exposed five defects. They were
intentionally not fixed during the test-safety-net phase because each changes
observable behavior.

## Requirements

1. **P4 edge export MUST be usable.**
   - `build_edge_from_link` MUST use numeric node IDs for `src_name` and
     `dst_name`, numeric interface suffixes for `src_port`/`dst_port`, and
     integer MAC values for `src_mac`/`dst_mac`.
   - Missing MACs or non-numeric interface suffixes MUST degrade to `0` rather
     than raising.
   - IPv4 endpoints MUST keep using the existing string-to-int conversion in
     `GraphSharedMemory`.
2. **Terminal bridge errors MUST go to stderr.**
   - `_log_error` MUST write the message to stderr and the per-process log
     file; it MUST NOT write errors to stdout.
3. **P4 shared-memory lifecycle MUST be leak-free.**
   - `P4Daemon` MUST accept an optional shared-memory name while keeping
     `satgonetem_graph` as the default.
   - Re-initialization, controller-start failure, and `remove()` MUST close and
     unlink any owned shared-memory segment and clear the reference.
   - `remove()` MUST also stop a running controller.
4. **Controller interface pattern MUST be honored.**
   - `get_management_interface` MUST format
     `controller_node_iface_pattern` with the controller interface index
     (`50000 + node_id`), preserving the current default (`eth50000` for node 0).
   - `NetworkConfig` MUST reject a pattern that does not contain
     `{node_id}`.
   - Documentation MUST state that the placeholder receives the controller
     interface index, not the raw node ID.
5. **Polar coverage geometry MUST be correct.**
   - `coverage_ring_from_elevation` MUST return a valid closed ring for
     satellites exactly over either pole.
   - Synthetic closure points MUST use geodetic pole latitudes, not ±180°.
   - `coverage_percentage` and `coverage_percentage_fast` MUST agree within
     1 percentage point at the pole.

## Acceptance criteria

- New tests fail on the legacy implementation and pass after the fixes.
- The strict xfail for polar coverage becomes a normal passing test.
- `ruff check satgonetem tests` and scoped `mypy` pass.
- The full default suite passes with the 65% coverage floor.
- No Docker, root, live networking, or real P4 process is required.

## Non-goals

- Enabling P4 in the routing registry or selecting it as a supported backend.
- Changing GoNetEm's controller interface numbering or the controller switch
  topology.
- Fixing unrelated geometry limitations at zero-altitude/zenith masks.

## Risks and mitigations

- **P4 export consumers:** the numeric interpretation must match the external
  reader. The fixed layout is documented in `docs/utils.md`; the new test pins
  IDs, ports, MAC integers, and gateway flags.
- **Controller pattern compatibility:** only patterns that produce the
  GoNetEm-generated `eth<controller-index>` interface can work end-to-end;
  documentation makes this explicit.
- **Polar polygon closure:** the projection is singular at the pole.
  Mitigation: append pole closure vertices and test against the spherical
  estimator.

## Verification

```bash
uv run --frozen --extra dev pytest -q tests/routing/test_p4_daemon.py tests/routing/test_p4_edges.py tests/utils/test_node_terminal_bridge.py tests/services/test_controller_commands.py tests/utils/test_coverage_geometry.py
uv run --frozen --extra dev ruff check satgonetem tests
uv run --frozen --extra dev mypy
uv run --frozen --extra dev pytest -q --cov=satgonetem --cov-fail-under=65
```

## Implementation result (2026-11-21)

- Added red tests first; 13 focused tests failed on the legacy behavior.
- P4 graph export now writes numeric node IDs, interface suffixes, and integer
  MACs; missing IP/MAC/interface values default safely.
- P4 shared memory is released on re-init, controller-start failure, and
  `remove()`; the name is configurable and `remove()` stops the controller.
- Controller interface patterns are formatted with the controller interface
  index and validated in `NetworkConfig`; docs updated.
- Terminal bridge errors now go to stderr and the log file.
- Polar coverage rings use valid pole closure vertices; exact-pole polygon and
  spherical coverage agree within 1 point.
- Full default suite: 1141 passed, 1 skipped, 9 deselected, 72.01% coverage in
  19.5 s; `ruff` and scoped `mypy` pass.
- Live P4 controller and GoNetEm/namespace behavior were not exercised.
