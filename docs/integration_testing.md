# Live integration testing

The live tier exercises the real Docker, GoNetEm gRPC, `tc`/`ip`, and Linux
namespace paths that the default unit suite mocks. It lives in
`tests/integration/` and is excluded from the default pytest run.

The contract for this tier is [Spec 0012](specs/0012-live-integration-tier.md).

## What runs where

| Module | Marker | Coverage |
|---|---|---|
| `test_live_smoke.py` | `integration` | GoNetEm version and images, three-node start, containers/PIDs, planned IPv4 addresses, static routes, GSL+ISL pings |
| `test_live_qos.py` | `integration` | Live `tc` TBF+netem state, `set_link_impairment`, `set_link_state` on both endpoints |
| `test_live_scale.py` | `integration`, `slow` | 3x3 Walker constellation with all 20 directed ground-station pings |
| `test_live_root_netns.py` | `integration`, `root` | Production `LinuxNetworkNamespace.create_veth` between two Alpine containers |

An `integration` test never runs in the default suite. A `root` test also
carries `integration`; a `slow` integration test is opt-in.

The 3x3 scale module pins the simulation start to 2026-11-03 15:00:00. The
sparse Walker shell only gives all five default ground stations a path to every
other at that epoch; the module checks all directed graph paths before starting
containers.

## Prerequisites

Run on Linux from the repository root with the locked environment:

- Python 3.10–3.14 through `uv`.
- A reachable Docker daemon. The current user must be able to use the Docker
  socket (usually membership in the `docker` group); root is not required for
  the smoke, QoS, or scale modules.
- A GoNetEm 0.9.x `gonetem-server` listening on `localhost:10110`, which matches
  the `NetworkConfig.gonetem_server` default.
- Required images:

  ```bash
  docker pull jariassuarez/sgnt:satellite
  docker pull jariassuarez/sgnt:ground-station
  docker pull alpine:3.20
  ```

- `ip` and `tc` on the host `PATH` (iproute2), used by the environment check
  and by assertions through `node.execute_command`. The root test also needs
  `nsenter` (util-linux).
- No other GoNetEm project should be running on the host while the tier runs:
  module topologies use unique project names, but a shared server and a shared
  Docker daemon are still global state.

The session fixture reports **every** failed prerequisite in one message,
including the exact remediation commands. It never skips.

### Root-only prerequisites

`test_live_root_netns.py` calls `setns` and creates a veth pair, so the process
needs effective `CAP_NET_ADMIN` and `CAP_SYS_ADMIN` (normally root). Run it
through the repository virtualenv so the project dependencies and imports
match:

```bash
sudo -E env PATH="$PATH" .venv/bin/python -m pytest tests/integration -q \
  -m "integration and root"
```

On a non-root host this fails with the same command in the message; it does not
skip. The root module is not part of the automated non-root CI job.

## Markers and selection

```bash
# Default suite: no live tests
uv run --frozen --extra dev pytest -q
uv run --frozen --extra dev pytest --collect-only -q   # collects 0 integration tests

# Non-root live tier (smoke + QoS); this is what CI runs
uv run --frozen --extra dev pytest tests/integration -q \
  -m "integration and not root and not slow"

# Root namespace test
sudo -E env PATH="$PATH" .venv/bin/python -m pytest tests/integration -q \
  -m "integration and root"

# Slow 3x3 scale smoke
uv run --frozen --extra dev pytest tests/integration -q -m "integration and slow"

# Everything live
uv run --frozen --extra dev pytest tests/integration -q -m integration
```

Integration runs do not apply a coverage floor. The coverage gate remains on
the unit suite, where results are deterministic.

## CI runner

[`integration.yml`](../.github/workflows/integration.yml) is a **non-required**
workflow triggered by `workflow_dispatch` and a nightly schedule. It is not
part of the repository ruleset's required checks.

Runner labels:

```text
[self-hosted, linux, x64, gonetem, docker]
```

Runner contract:

- Linux x86_64 with Docker, `ip`, and `tc` installed.
- A pre-provisioned `gonetem-server` 0.9.x on `localhost:10110`; the workflow
  does not install or build GoNetEm.
- The three required images pulled locally.
- The runner user can access the Docker socket without an interactive
  password; the non-root job does not use sudo.
- The host is dedicated to this workflow. The cleanup step intentionally
  removes every container whose name starts with `ntm` because GoNetEm uses
  that prefix. This is safe only while the job is serialized (one concurrency
  group per ref, `cancel-in-progress: false`) and no unrelated GoNetEm project
  shares the runner. Never point this workflow at a shared host with other
  GoNetEm users.
- If the server writes to a non-default log file, export `GONETEM_SERVER_LOG`
  on the runner so the diagnostics step can copy it; the step also attempts
  `journalctl -u gonetem-server`.

The job runs with `timeout-minutes: 30` and always uploads `integration.xml`,
the pytest log, `docker ps -a`, and the collected GoNetEm logs. The cleanup
step runs with `if: always()` so a failed startup does not strand containers.

## Cleanup and troubleshooting

GoNetEm names project containers `ntm<project_id>.<node>`. Tests assert and
clean up with that exact prefix, never a bare `ntm` match:

```bash
# Inspect leftovers (all projects)
docker ps -a --format '{{.Names}}' | grep -E '^ntm'

# After a test run, this must be empty
docker ps -a --format '{{.Names}}' | grep -E '^ntm'
```

Each module-scoped topology fixture tears down in `try/finally`:

1. `stop_gonetem()` is attempted.
2. If it raises, `force_stop_gonetem()` is attempted.
3. The exact `ntm<project_id>` prefix is swept and every remaining container is
   force-removed.
4. Every stop, list, and removal failure is recorded and raised at the end of
   teardown. Cleanup errors are never swallowed.

Common failures:

| Symptom | Action |
|---|---|
| Prerequisite report lists Docker | Start Docker and grant socket access (docker group) |
| Prerequisite report lists `ServerGetVersion` | Start `gonetem-server` on `localhost:10110` |
| Prerequisite report lists an image | Run the `docker pull` commands above |
| `static routing initialisation failed` | Check GoNetEm server logs and container startup; the fixture reports leftover containers during teardown |
| `ping ... did not finish` | Check the ping result and `tc qdisc show`; deadlines are generous but a stranded container blocks replies |
| Root test fails with `sudo -E ...` | Run the exact command from the failure as a user with the required capabilities |
| Cleanup error names containers | `docker rm -f` those exact `ntm<id>.*` names; report the project id from the failure |

## Why skips are forbidden

A skipped live test looks green while proving nothing about Docker, GoNetEm,
qdiscs, or namespaces. The tier therefore treats every missing prerequisite as
a hard failure with an actionable report. Do not add `pytest.skip` to these
modules or weaken an assertion to make a bad environment pass; fix the
environment or the harness.
