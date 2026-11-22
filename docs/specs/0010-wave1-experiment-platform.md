# 0010 — Wave 1 experiment platform and link controls

- Status: Implemented
- Date: 2026-11-22
- Implementation date: 2026-11-22
- Depends on: 0004, 0005, 0006, 0007, 0009
- Supersedes: none

## Context

SatGoNetem can emulate a constellation, but running an experiment still
requires bespoke Python: there is no console entry point, no run manifest, no
unified metrics export, and no scheduled fault injection. The GoNetEm backend
already exposes control RPCs that the Python client does not use. Links also
lack loss/jitter fields even though the backend protocol supports them.

Wave 1 makes the tool usable for repeatable experiments without changing the
core emulation model. IPv6 addressing is specified separately in 0011.

## Requirements

1. **CLI.** Add a `satgonetem` console script with `version`, `doctor`,
   `summary`, `create`, `edit`, `run`, and `report` subcommands. `doctor` must
   check the Python version, Docker availability, the GoNetEm endpoint, and the
   presence of a sample topology. `summary` must work offline against a
   topology file. `create` and `edit` open an interactive scenario shell that
   can add Walker shells, ground stations, traffic, events, and network
   overrides; they must not shell out to a text editor.
2. **Scenario manifest.** Support a YAML file describing a topology *or*
   Walker shells plus ground stations, a tick interval, routing method,
   optional network overrides, traffic specs, event specs, metrics interval,
   and output directory. Validation errors must name the offending field.
3. **Run directory.** Every run writes `manifest.yaml`, `metrics.jsonl`, and
   `summary.json`; generate a human-readable `report.md` from those artifacts.
4. **Telemetry.** Provide a `MetricsCollector` that samples topology counts,
   per-link state (active, delay, capacities, loss, jitter, tx/rx when
   available), simulation step/time, and the last simulation error. Export
   JSONL and JSON.
5. **Connectivity matrix.** Expose a public `connectivity_matrix()` returning
   typed per-pair results (reachable, loss, RTT min/avg/max, error) instead of
   printing a summary. Persist the matrix in the run directory when requested.
6. **Backend controls.** Expose `restart_node`, `set_interface_state` (or
   `set_link_state`), `stop_all_nodes`, `start_all_nodes`,
   `capture_interface`, `get_backend_status`, and `save_snapshot` through the
   launcher and `TopologyManager`. All must check RPC status and raise on
   application errors.
7. **Link impairment fields.** Add optional `loss` (percent, 0–100) and
   `jitter` (milliseconds, non-negative) to `Link`; include them in generated
   GoNetEm YAML and in `LinkUpdate`/`LinkUpdateMany` requests. Add
   `NetworkConfig` defaults for new links.
8. **Packaging.** Add `[project.scripts]`, package discovery that works from a
   `git archive` build, package data for sample topologies, and `py.typed`.
9. **Compatibility.** Existing Python API calls and generated projects keep
   working. New CLI/telemetry modules must not require Docker at import time.

## Acceptance criteria

- `satgonetem --help`, `satgonetem version`, and `satgonetem summary
  topology_files/test_topology.json` succeed without Docker.
- `satgonetem create demo` creates a valid shell-based scenario and opens the
  interactive shell; `satgonetem edit demo.yaml` loads and saves it.
- `satgonetem doctor` exits 0 when prerequisites are present and non-zero with
  actionable messages when they are not.
- A scenario run creates the four artifacts and a report that includes the
  topology summary and at least one sampled link metric.
- `connectivity_matrix()` returns one result per ordered node pair and
  serializes to JSON.
- Unit tests cover fake launcher responses for every new RPC, including a
  non-OK `Status.code`.
- Generated YAML and gRPC requests preserve non-zero loss/jitter.
- `uv build` from a clean `git archive` produces a wheel containing the CLI
  entry point, `py.typed`, and the sample topology files.

## Non-goals

- IPv6 routing or IPv6 traffic generation (addressing only; see 0011).
- A web dashboard; the read-only API can follow in a later spec.
- Sub-second tick precision; scenario ticks use the existing integer seconds.
- Replacing `FlowScheduler` wall-clock semantics.

## Risks and mitigations

- **Live-backend tests require Docker/root.** Keep all new orchestration
  injectable and test with fakes; report live checks separately.
- **Scenario runner drift from `TopologyManager`.** Keep the runner thin and
  call public methods only.
- **Packaging changes break installs.** Add a wheel smoke test that imports
  the CLI and resolves a sample topology.

## Verification

```bash
uv run --frozen --extra dev pytest -q tests/cli tests/experiment tests/telemetry
uv run --frozen --extra dev pytest -q tests/launchers tests/models tests/services
uv run --frozen --extra dev ruff check satgonetem tests
uv run --frozen --extra dev mypy
uv build --out-dir dist
python -c "import satgonetem; import satgonetem.cli.main"
```

### Verification record — 2026-11-22

- `pytest -q` — 1976 passed, 1 skipped, 9 deselected (Python 3.14).
- Interactive `create`/`edit` tests cover shell/ground-station/traffic/event
  entry, save validation, overwrite protection, and the default filename.
- `pytest -q tests/cli tests/experiment tests/telemetry tests/test_connectivity.py tests/test_packaging.py` — 37 passed under Python 3.10.
- `ruff check satgonetem tests` — pass; `ruff format --check` — pass.
- `mypy` — pass (116 files).
- `uv lock --check` — pass.
- `uv build` — wheel contains the CLI, `py.typed`, and bundled topology data;
  a wheel installed in a clean venv resolves `test_topology.json`.
- Unverified: live Docker/GoNetEm behavior for the full RPC surface. A live
  three-node check validated `ProjectGetStatus`, `NodeSetIfState` (both
  directions), `LinkUpdate` loss/jitter, `ProjectSave`, bounded `NodeCapture`
  (1208 bytes returned on deadline), and `NodeRestart`; `TopologyStopAll`/
  `TopologyStartAll` were not exercised live.
