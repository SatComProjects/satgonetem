# 0007 — Launchers, builders, and utilities

- Status: Implemented
- Date: 2026-11-21
- Depends on: 0001, 0002, 0003
- Supersedes: —

## Context

The `NetworkLauncher` ABC and `GoNetEmLauncher` disagree on return values and
accepted arguments; `start_gonetem()` constructs the concrete launcher directly,
so `network_launcher` is metadata only. `gonetem_launcher.py` (777 lines) mixes
YAML generation, gRPC, lifecycle, and Docker fallback. `project_builder.py`
(904 lines) mixes configuration models, filesystem I/O, TLE/CSV ingestion, and
sample generation. `utils` contains domain-specific modules (`test.py`,
`coverage.py`), a CLI bridge, shared-memory graph transport, and an
import-time compatibility patch.

## Requirements

- **MUST** align the `NetworkLauncher` ABC and concrete implementations on
  return types and signatures; add a contract suite and a fake launcher.
- **MUST** obtain launchers through a factory/registry and make
  `network_launcher` select the backend.
- **MUST** split `gonetem_launcher.py` into project/YAML generation, a gRPC
  client adapter, and lifecycle orchestration.
- **MUST** split `project_builder.py` into configuration/serialization,
  TLE/CSV ingestion, and sample-project construction, with explicit filesystem
  access.
- **MUST** relocate or rename misplaced utilities behind compatibility facades:
  geodesy module, satellite coverage, terminal bridge, shared-memory graph,
  compatibility patch.
- **MUST** keep `utils/satcom_fix.py` import-time patching behavior unless a
  separate spec removes it; make it idempotent and contract-tested.
- **MUST** keep packaging metadata in `pyproject.toml`.

## Acceptance criteria

- A dummy launcher works through the public lifecycle without caller changes,
  and `network_launcher` selects it.
- `HILManager` conforms to its tested contract with external commands behind
  adapters.
- `project_builder` has no hidden CWD assumptions and at least 75% coverage.
- Old utility import paths remain aliases for one release cycle where renamed.
- The wheel builds and imports after a packaging change.

## Non-goals

- Removing the `sat_com_trajectopy` patch.
- New launcher backends beyond a test fake.

## Risks and mitigations

- **Public import breakage:** retain thin facades and document deprecations.
- **YAML compatibility:** characterize generated project files before moving
  generation code.
- **Import-time patch ordering:** test direct imports and cold imports.

## Verification

```bash
uv run --frozen --extra dev pytest -q tests/launchers tests/utils
uv run --frozen --extra dev pytest -q --cov=satgonetem/launchers --cov-fail-under=75
uv build --out-dir dist
```

## Implementation result (2026-11-21)

- Aligned `NetworkLauncher.start_containers` with the concrete signature and
  added `force_close_project`; added a fake launcher contract test.
- Added `launchers/registry.py`; `network_launcher` now selects the backend.
  The GoNetEm class remains dynamically resolved so existing patch points work.
- Extracted GoNetEm project/YAML generation to `gonetem_project.py`; the
  launcher delegates and keeps its public methods.
- Split the remaining GoNetEm launcher into `gonetem_client.py` (gRPC/link
  requests), `gonetem_lifecycle.py` (start/wire/link/close), and
  `gonetem_cleanup.py` (Docker force-close); the launcher is now a thin
  composition (~85 lines).
- Split `project_builder.py` into `project_ground_objects.py`,
  `project_mesh.py`, and `project_fixtures.py`; `project_builder` (~300 lines)
  keeps custom-object construction and re-exports the public helpers.
- Moved geodesy, satellite coverage, shared-memory graph, and terminal bridge
  code to `geodesy.py`, `satellite_coverage.py`,
  `routing/graph_shared_memory.py`, and `cli/node_terminal_bridge.py`; legacy
  module paths remain compatibility facades.
- Launcher coverage is 75.86%; full suite 1224 passed, 79.91% coverage.
- Packaging remains single-source in `pyproject.toml`; `setup.py` is gone.
- `HILManager` continues to conform through the Phase 5 command port.
