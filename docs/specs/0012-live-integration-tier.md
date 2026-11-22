# 0012 — Live integration test tier

- Status: Implemented
- Date: 2026-11-22
- Depends on: 0002, 0003, 0008
- Supersedes: —

## Context

The default suite is fast and deterministic, but every Docker, GoNetEm, gRPC,
and Linux-namespace path is mocked. The existing `integration` marker is
declared and never used, and `addopts` excludes only `slow` and `benchmark`, so
a new live test would silently join all five unit-test CI legs. Manual scripts
(`scripts/verify_connectivity.py`, `verify_10x10_connectivity.py`) are the only
live evidence and do not assert cleanup or run automatically.

The local development host has Docker 29.9.3, GoNetEm 0.9.1 on
`127.0.0.1:10110`, and the satellite/ground-station images. It is a non-root
user without passwordless sudo, so the non-root live tier can be executed
locally; root-only namespace tests cannot.

## Requirements

- **MUST** exclude `integration` from the default suite: `addopts` must select
  `not slow and not benchmark and not integration`.
- **MUST** add a `root` marker for tests that additionally require root or
  effective `CAP_SYS_ADMIN`/`CAP_NET_ADMIN`. `root` tests MUST also carry
  `integration`.
- **MUST** add `tests/integration/` with:
  - a session-scoped live environment fixture that fails with an actionable
    prerequisite report (never silently skips) when Docker, GoNetEm, images,
    `ip`, or `tc` are missing;
  - a module-scoped small-topology fixture that starts GoNetEm, yields the
    topology, and always tears down;
  - a root-capability fixture that fails with the exact `sudo -E` command when
    the capability is absent.
- **MUST** share live helpers from `tests/support/live.py`, not duplicate them
  in test modules or import the untracked manual scripts.
- **MUST** cover, at minimum:
  1. GoNetEm gRPC `ServerGetVersion` succeeds and required Docker images exist.
  2. A three-node topology starts, every node has a container and PID, planned
     IPv4 addresses are applied, static routing installs, and GSL+ISL pings in
     both directions receive replies with zero loss.
  3. Teardown leaves no containers with the project prefix; a failed teardown
     is reported, not swallowed.
- **SHOULD** cover link QoS/impairment against the live backend and a small
  Walker scale smoke test marked `integration and slow`.
- **MAY** cover a root-only `setns`/veth round trip marked
  `integration and root`; it is not part of the non-root CI job.
- **MUST** add a non-required `integration` CI workflow on a self-hosted runner
  label (`[self-hosted, linux, x64, gonetem, docker]`) triggered by
  `workflow_dispatch` and a nightly schedule. It must not be added to the
  required ruleset checks in this change.
- **MUST** upload JUnit XML, the pytest log, `docker ps -a`, and GoNetEm server
  logs on failure, and run a project-prefix cleanup step with `if: always()`.
- **MUST** document prerequisites, marker selection, local commands, and CI
  runner labels in `docs/integration_testing.md` and link them from the README.
- **MUST NOT** change production behavior, the unit suite runtime, or the
  coverage gate. Integration tests run without `--cov-fail-under`.

## Acceptance criteria

- `pytest --collect-only -q` collects zero `integration` tests by default.
- `pytest tests/integration -q -m "integration and not root and not slow"`
  passes on a host with the documented prerequisites, and fails with a single
  readable prerequisite report when they are absent.
- The live smoke test starts three containers, pings GSL+ISL in both
  directions, stops the project, and asserts no `ntm*` leftovers for the
  project prefix.
- `pytest tests/integration -q -m integration` on a non-root host fails the
  root-marked tests with an actionable `sudo -E` command, not a skip.
- `ruff check satgonetem tests`, `ruff format --check`, `mypy`, and the full
  default suite stay green.
- The new workflow passes `actionlint` and is not a required status check.

## Non-goals

- Installing/building GoNetEm in GitHub-hosted CI; the job targets a
  pre-provisioned self-hosted runner.
- Running the 10x10/105-container script on every PR.
- Replacing the existing manual scripts; they remain useful for ad hoc runs.
- HIL bridge/root networking coverage beyond the narrow namespace test.
- Changing GoNetEm, Docker, or the static routing implementation.

## Risks and mitigations

- **Prerequisite masking:** a skip makes an unverified live path look green.
  Mitigation: `pytest.fail(..., pytrace=False)` with a complete diagnostic.
- **Cleanup leaks:** failed startup or `concurrency.cancel-in-progress` can
  strand containers. Mitigation: `try/finally`, `force_stop_gonetem`, exact
  project-prefix cleanup, `if: always()` workflow step, and a dedicated
  concurrency group that does not cancel in progress.
- **Flaky timing:** ARP warm-up and netem rounding. Mitigation: one warm-up
  ping, 3-5 measured packets, monotonic deadlines, 2% relative / 2 ms absolute
  RTT tolerance, and generous startup timeouts.
- **Hosted-runner cost:** GoNetEm plus ~2.8 GB images is slow and fragile.
  Mitigation: self-hosted runner and non-required job until stable.
- **Shared live state:** one GoNetEm server and project-prefix container names.
  Mitigation: unique project name per test module, no session-scoped mutable
  topology, serialize integration jobs.
- **Coverage interaction:** live coverage is nondeterministic. Mitigation: keep
  the unit coverage gate; integration runs without a coverage floor.

## Verification

```bash
# Default suite excludes integration
uv run --frozen --extra dev pytest --collect-only -q -m integration

# Live smoke (non-root)
uv run --frozen --extra dev pytest tests/integration -q \
  -m "integration and not root and not slow"

# Root-only namespace test
sudo -E env PATH="$PATH" .venv/bin/python -m pytest tests/integration -q \
  -m "integration and root"

# Static gates
uv run --frozen --extra dev ruff check satgonetem tests
uv run --frozen --extra dev ruff format --check satgonetem tests
uv run --frozen --extra dev mypy
uv run --frozen --extra dev pytest -q
```

## Implementation result

Implemented on 2026-11-22.

Delivered:

- `pyproject.toml`: default `addopts` now select
  `not slow and not benchmark and not integration`; declared the `root` marker.
- `tests/support/live.py`: `LiveEnvironment`, `require_live_environment()`
  (all failures plus remediation, `pytest.fail(..., pytrace=False)`), unique
  project names, exact `ntm<project_id>` container matching, monotonic
  `wait_for_flow()`, zero-loss/RTT-bounded `assert_ping()`, and a teardown that
  reports every stop/sweep failure.
- `tests/integration/conftest.py`: session `live_env`, module-scoped
  `live_topology` (two TLE satellites + Paris, one ISL, one GSL, planned
  addresses, static routing), and `require_root` with the exact `sudo -E`
  command.
- Live modules: three-node smoke, live TBF+netem/impairment/link-state checks,
  a 3x3 Walker scale smoke (2026-11-03 15:00 epoch, 20/20 directed GS paths),
  and a root-only `create_veth`/`nsenter` test.
- `.github/workflows/integration.yml`: nightly/manual non-required job on
  `[self-hosted, linux, x64, gonetem, docker]`, JUnit/log/`docker ps -a`/
  GoNetEm-log artifacts, serialized concurrency, and an `if: always()`
  `ntm`-prefix cleanup step. `.github/actionlint.yaml` declares the two custom
  runner labels so actionlint accepts the workflow.
- `docs/integration_testing.md` and README updates (Testing section, CI table,
  technical-debt row).

Validation evidence:

| Command | Result |
|---|---|
| `uv run --frozen --extra dev pytest --collect-only -q` | exit 0; `2150/2167 collected (17 deselected)`; zero `tests/integration` paths |
| `uv run --frozen --extra dev pytest tests/integration -q -m "integration and not root and not slow"` | exit 0; `6 passed, 2 deselected` (smoke + QoS); no `ntm` containers afterwards |
| `uv run --frozen --extra dev pytest tests/integration/test_live_scale.py -q -m "integration and slow"` | exit 0; `1 passed` (3x3, 14 containers, 20 directed pings); no leftovers |
| `uv run --frozen --extra dev pytest tests/integration -q -m "integration and root"` | exit 1 as required; setup error reports `effective uid is 1000, not 0; CAP_NET_ADMIN ...; CAP_SYS_ADMIN ...` and the exact `sudo -E env PATH="$PATH" .venv/bin/python -m pytest tests/integration -q -m "integration and root"` command |
| `docker ps -a --format '{{.Names}}' \| grep -E '^ntm'` | empty after each live run |
| `uv run --frozen --extra dev ruff check satgonetem tests` | exit 0 |
| `uv run --frozen --extra dev ruff format --check satgonetem tests` | exit 0 |
| `uv run --frozen --extra dev mypy` | exit 0; no issues in 135 source files |
| `uvx --from actionlint-py actionlint` | exit 0 |
| `uv run --frozen --extra dev pytest -q` | exit 0; `2149 passed, 1 skipped, 17 deselected` |

Unverified:

- The root-only namespace test could not be executed on this host: the user has
  no passwordless sudo and no effective `CAP_NET_ADMIN`/`CAP_SYS_ADMIN`. Its
  non-root failure path was verified; the successful path is unverified.

