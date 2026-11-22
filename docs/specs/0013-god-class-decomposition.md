# 0013 — Decompose the four largest classes

- Status: Implemented
- Date: 2026-11-22
- Depends on: 0003, 0004, 0005, 0006, 0012
- Supersedes: —

## Context

Four classes have grown beyond a maintainable size despite earlier splits:

| File | Class | Size | Independent maps |
|---|---|---:|---|
| `traffic/iperf3_results.py` | `Iperf3Results` | 832 lines | metric parsing + presentation + plotting |
| `routing/static_daemon.py` | `StaticRoutingDaemon` | 1,217 lines | graph planning + iface cache + gRPC transport |
| `routing/srmpls_daemon.py` | `SRMPLSDaemon` | 1,165 lines | SID planning + three apply passes + teardown |
| `services/topology_satcom.py` | `TopologyManager` | 1,313 lines | config + persistence + project adapter + link factory |

Specs 0004, 0005, and 0006 already track these remaining cohesion exceptions.
This specification completes them as behaviour-preserving extractions. It is a
structural refactor: no routing, lifecycle, locking, caching, serialization, or
metric behaviour changes.

## Requirements

### General

- **MUST** preserve every public class, method, property, constructor
  signature, module import path, and `__all__` export. ADR 0005 facades apply.
- **MUST** keep thin delegating shims for every private member that existing
  tests patch, call, or read directly. Extracted collaborators must not require
  `__init__` to have run when the existing tests construct bare instances.
- **MUST** add direct unit tests for each new collaborator before or alongside
  its extraction, pinning the contract that was previously only exercised
  indirectly.
- **MUST** preserve object identity and aliasing where current behaviour
  depends on it: cached route lists, previous-table lists, combinator caches,
  `_failed_nodes`, `_failed_link_adds`, and injected strategies.
- **MUST NOT** change pass order, worker sizing/floors, lock acquisition,
  retry/fallback semantics, command strings, JSON keys, date formats, or
  numeric conversions.
- **MUST NOT** weaken, skip, or delete an existing test. If a test fails, fix
  the extraction.
- **MUST NOT** add dependencies or new abstraction layers without at least one
  concrete second consumer (rule of three).
- **MUST** update the module maps in `docs/routing.md`, `docs/services.md`,
  `docs/traffic.md`, and the README architecture section when module layout
  changes.

### Size acceptance targets

- `traffic/iperf3_results.py` **MUST** be at most 350 lines.
- `routing/static_daemon.py` **MUST** be at most 600 lines.
- `routing/srmpls_daemon.py` **MUST** be at most 650 lines.
- `services/topology_satcom.py` **MUST** be at most 650 lines.
- No new module **MUST** exceed 450 lines. If a collaborator would exceed it,
  split along the responsibility boundary or keep that responsibility in the
  facade.
- Line counts are a proxy, not the goal: each extracted module must have one
  stated responsibility and no circular dependency on its facade.

### `Iperf3Results` (traffic)

- **MUST** extract:
  - `iperf3_units.py` — unit constants; replace duplicated literals only.
  - `iperf3_report.py` — `print_summary`, `to_json`, `__repr__` formatting.
  - `iperf3_end_summary.py` — sender/receiver/bidir end-section semantics.
  - `iperf3_intervals.py` — interval flattening, DataFrames, rate/series math.
  - `iperf3_plotting.py` — metric map, `plot_metric`, `plot_all`.
- **MUST** preserve `to_json()["raw"] is` the parsed dict, the six `config`
  fields, exact DataFrame columns/`_t`/`_label`, `.copy()` accessors, mutable
  `json_side`/`client_notes`, and all bits/bytes, seconds/ms/us conversions.
- **MUST NOT** consolidate `iperf3_plots.generate_iperf3_plots`; its
  omitted-interval mismatch is a separate behaviour change.

### `StaticRoutingDaemon` (routing)

- **MUST** extract `static_route_ops.route_cache_signature`,
  `PeerInterfaceIndex`, `StaticRoutePlanner`, and `RouteBatchInstaller`.
- **MUST** preserve cache list identity/aliasing on no-op updates, the single
  shared `_failed_nodes` set, sticky `_route_install_supported`, call-time
  `_exec_batch`/chunk-limit injection, and exact `ip route`/`sh -lc` command
  strings.
- **MUST** keep the legacy NetworkX and igraph paths distinct; they are not
  unified in this refactor.
- **MAY** extract `StaticRouteTableSynchronizer` only if the facade still
  exceeds the size target; table ownership is a defensible remaining boundary.

### `SRMPLSDaemon` (routing)

- **MUST** extract `srmpls_route_ops` (pure command identity/diff/batch),
  `SRMPLSTeardown`, `SRMPLSRoutePlanner` (all-pairs and per-source planners),
  and `SRMPLSRouteInstaller` (enable + three apply passes + per-source install).
- **MUST** preserve pass order `enable → SID → GS → satellite stack`; the SID
  pass reads the previous stack cache and the stack pass reads the freshly
  committed SID cache.
- **MUST** preserve cache dict object identity (pass by reference, never
  rebind), worker floors and empty-class guards, the GS full-string diff versus
  the SID/stack key diff, scoped `remove(node)` state retention, the
  `gonetem_is_on` early return, and exact batch strings.
- **MUST NOT** remove or rewrite the vestigial label-stack cache; that is a
  separate cleanup spec.

### `TopologyManager` (services)

- **MUST** extract:
  - `services/network_config.py` — `NetworkConfig`, `MAX_LINK_QUEUE_PACKETS`,
    `config_from_instance`, `apply_config`.
  - `services/topology_persistence.py` — runtime-state serialize/restore,
    ground-file embedding/restore, and file payload assembly.
  - `services/satcom_project.py` — simulation loading and tick-action
    validation.
  - `services/satcom_link_factory.py` — SatCom/test link construction and
    distance refresh, with `TopologySyncMixin` delegates retained for
    `DummySync` overrides.
- **MAY** extract a graph/change view for `get_current_graph` and
  `check_for_updates`/hashes if required by the size target.
- **MUST** preserve `NetworkConfig` field names, defaults, declaration order,
  `__post_init__` validation, and `from_mapping` tolerance; old payloads must
  restore with defaults.
- **MUST** preserve the exact payload keys
  (`simulation_property`, `network_config`, `ground_files`, optional
  `runtime_state`), runtime-state shapes, `%d/%m/%Y %H:%M:%S`, tolerant
  warning-and-skip restore, and ground-file path sanitization.
- **MUST** keep public settable attributes (`satellites`, `ground_stations`,
  `links`, `status`, `use_file_routes`, `project_name`, `current_time_step`,
  `address_plan`, `address_plan_v6`, `project_id`, `client`, `request`,
  `update_time`, `update_factor`) and the `RoutingContext` surface.
- **MUST** initialize every collaborator before `init()` runs, and keep the
  `_failed_link_adds` eager-initialized shared set.
- **MUST NOT** add locks or change lock ordering. The existing unlocked
  `to_file`/`get_current_graph` snapshot limitation stays; document it rather
  than silently changing concurrency semantics.

## Acceptance criteria

- The four target files meet their line budgets; `wc -l` evidence is recorded.
- Public imports continue to work from the old module paths.
- `pytest -q` passes with the same or a larger test count; no test is skipped
  or weakened.
- Coverage does not regress by more than 0.5 percentage points in
  `satgonetem/traffic`, `satgonetem/routing`, and `satgonetem/services`.
- `ruff check satgonetem tests`, `ruff format --check satgonetem tests`, and
  `mypy` pass.
- The live integration smoke tier from Spec 0012 passes on the documented
  environment.
- `git diff` shows no behaviour edits outside the extraction and its tests.

## Non-goals

- Fixing cache/stale-route semantics, the unused label-stack cache, the GS
  churn behaviour, or the plotting omitted-interval mismatch.
- Strict-mypy burn-down, new protocols, or mixin architecture changes.
- Rewriting command generation in `models/mpls_entry.py` or
  `routing/static_route_ops.py`.
- Adding async/concurrent execution or changing lock semantics.

## Risks and mitigations

- **Patch seams:** existing tests patch private methods and module constants.
  Mitigation: keep delegation shims and resolve callbacks/limits at call time.
- **Object identity:** route reuse and `FailedNode` retry depend on identity.
  Mitigation: pass the same objects and add identity assertions where missing.
- **Serialization drift:** persistence restores old files. Mitigation: pin
  payload keys and round-trip a legacy fixture before moving the codec.
- **MRO/import cycles:** collaborators must remain leaf-ward. Mitigation:
  return models/SimulationManager, never import the facade.
- **Large-bang churn:** mitigate with one class per commit and one responsibility
  per sub-step; run targeted tests after every sub-step.

## Verification

```bash
# Per class
uv run --frozen --extra dev pytest -q tests/traffic
uv run --frozen --extra dev pytest -q tests/routing
uv run --frozen --extra dev pytest -q tests/services tests/addressing

# Whole change
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev pytest -q --cov=satgonetem --cov-branch
uv run --frozen --extra dev ruff check satgonetem tests
uv run --frozen --extra dev ruff format --check satgonetem tests
uv run --frozen --extra dev mypy
uv run --frozen --extra dev pytest tests/integration -q \
  -m "integration and not root and not slow"
wc -l satgonetem/traffic/iperf3_results.py \
  satgonetem/routing/static_daemon.py \
  satgonetem/routing/srmpls_daemon.py \
  satgonetem/services/topology_satcom.py
```

## Implementation result (2026-11-22)

| Facade | Before | After | Extracted collaborators |
|---|---:|---:|---|
| `traffic/iperf3_results.py` | 832 | **255** | `iperf3_end_summary`, `iperf3_intervals`, `iperf3_report`, `iperf3_plotting`, `iperf3_units` |
| `routing/static_daemon.py` | 1,217 | **530** | `static_route_planner`, `peer_interface_index`, `route_batch_installer`, `static_route_ops` |
| `routing/srmpls_daemon.py` | 1,165 | **647** | `srmpls_planner`, `srmpls_installer`, `srmpls_teardown`, `srmpls_route_ops` |
| `services/topology_satcom.py` | 1,313 | **635** | `network_config`, `topology_persistence`, `satcom_project`, `satcom_link_factory`, `topology_graph` |

The largest extracted module is `services/topology_persistence.py` at 435
lines; every new module is at most 450. Public/private compatibility shims
were retained for all names used by existing tests. An AST parity check
confirmed no public method disappeared from the four classes and
`NetworkConfig` retains all 34 fields in their original order.

Validation on the reconciled tree:

| Command | Result |
|---|---|
| `pytest -q` | exit 0; 2,149 passed, 1 pre-existing skip, 17 deselected |
| `pytest -q --cov=satgonetem --cov-branch` | exit 0; **88.42%** branch coverage (from 87.15%) |
| `ruff check satgonetem tests` / `ruff format --check` | exit 0 |
| `mypy` | exit 0; no issues in 135 source files |
| `pytest tests/integration -q -m "integration and not root and not slow"` | exit 0; 6 passed |
| `pytest tests/integration/test_live_scale.py -q -m "integration and slow"` | exit 0; 1 passed |
| `pytest tests/integration -q -m "integration and root"` | exit 1 as required with the exact `sudo -E` remediation; no containers left |

No behavior changes were accepted: one `apply_config` tolerance drift and one
`to_file` ordering drift found during review were reverted to the `HEAD`
semantics before validation.
