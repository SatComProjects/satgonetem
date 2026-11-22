# Refactor Plan: SOLID + TDD Adoption

| | |
|---|---|
| Status | Proposed |
| Branch | `develop` |
| Baseline commit | `9505aae` (develop, 2026-11-21) |
| Working agreement | [AGENTS.md](../AGENTS.md) |
| Scope | Behavior-preserving structural refactor and quality-gate adoption |
| Out of scope | New features, public API redesign, dependency upgrades unrelated to the plan |

## 1. Executive summary

The codebase already has useful seams — `RoutingDaemon` and `NetworkLauncher`
base classes, capability mixins, strategy objects, and self-contained domain
packages. The refactor is therefore **incremental hardening, not a rewrite**.

The main problems are:

- quality gates are mostly declarative: ruff checks four narrow rule families,
  mypy/black/flake8 are installed but never run, coverage has no threshold, and
  branch protection does not require checks;
- several high-risk modules have little or no test coverage;
- external systems (Docker, gRPC, `subprocess`, network namespaces) are called
  directly from domain and orchestration code, so tests must patch internals;
- a few god modules and hard-coded factories concentrate unrelated
  responsibilities;
- tests duplicate scaffolding instead of sharing fakes and builders.

The plan fixes the process first, then builds a test safety net, then moves
boundaries behind explicit ports, then decomposes subsystems one at a time.
Every phase is independently shippable and behavior-preserving.

## 2. Measured baseline

Captured on `develop` at `9505aae` with the locked environment; the current
column was refreshed on 2026-11-22 after the launcher/utils safety-net work.

### 2.1 Tests and static analysis

| Signal | Baseline | Current (2026-11-22) |
|---|---|---|
| Test files / test functions | 65 / 807 | 100 / 1,222 |
| Suite result | **862 passed, 1 skipped, 82 warnings in 55.86 s** | **1,416 passed, 1 skipped, 9 deselected in 17.2 s** |
| Coverage | **62%** (8,260 statements, 3,122 missed) | **85.1%** (8,140 statements, 1,211 missed) |
| Ruff | passes; `select = ["E9", "F63", "F7", "F82"]` only | passes; ratchet includes `B`, `E4`, `E7`, `E9`, `F`, `I`, `SIM`, `UP` |
| mypy | not configured, not run in CI; **16 errors** in `models`+`addressing`+`link_budget`, **293 repo-wide** (`--ignore-missing-imports`) | configured repo-wide, run in CI: **0 errors in 103 files** |
| black / flake8 | installed, not configured, not run in CI | superseded by `ruff format` |
| Coverage gate | none (XML artifact only) | `fail_under = 75` locally and in CI |
| Integration/unit separation | none; no markers, no shared `conftest.py` | markers, `--strict-markers`, shared `conftest.py` + `tests/support/` |
| Branch protection | `main`: deletion/non-fast-forward only; `develop`: none | unchanged in this refresh (not re-verified) |

CI runs lint, tests on Python 3.10–3.14, and package build. The release
workflow does not depend on lint/test, so a tag on a red commit can publish.

### 2.2 Coverage hotspots

Refreshed 2026-11-22. `launchers/` is now at 100% and `utils/` at 100% except
for one unreachable defensive guard (`utils/project_mesh.py:147`). Remaining
hotspots are outside those packages:

| Module | Coverage |
|---|---|
| `models/mixins/qos_mixin.py` | 19% |
| `cli/node_terminal_bridge.py` | 31% |
| `ports.py` | 51% |
| `services/topology_satcom.py` | 63% |
| `routing/srmpls_daemon.py` | 64% |
| `services/mixins/routing_mgr.py` | 65% |
| `services/mixins/topology_sync.py` | 73% |
| `services/mixins/link_ops.py` | 73% |
| `routing/isis_sr_bird_daemon.py` | 76% |
| `services/mixins/controller.py` | 77% |
| `models/node.py` | 77% |
| `services/mixins/diagnostics.py` | 78% |

### 2.3 Structural hotspots

| Module | Lines | Observed responsibility overload |
|---|---|---|
| `traffic/iperf3_utils.py` | 1,691 | command building, execution, parsing, plotting, flow lifecycle |
| `routing/srmpls_daemon.py` | 1,320 | route computation, label management, setup/teardown, parsing |
| `routing/static_daemon.py` | 1,245 | route computation, command batching, installation, verification |
| `services/topology_satcom.py` | 1,121 | configuration, lifecycle, sync, routing, traffic, diagnostics |
| `utils/project_builder.py` | 904 | config models, YAML/CSV I/O, TLE handling, sample generation |
| `launchers/gonetem_launcher.py` | 777 | gRPC client calls, project YAML generation, lifecycle |
| `services/mixins/topology_sync.py` | 773 | sync, link diffing, distance batching, satcom conversion, 8-way key type switch |
| `services/mixins/link_ops.py` | 591 | gRPC link operations, HIL branching, stats, local mutation |
| `models/node.py` | 484 | domain state **plus** Docker exec and Linux namespace/veth management |
| `services/mixins/diagnostics.py` | 679 | diagnostics **plus** direct `subprocess` process control |
| `services/mixins/controller.py` | 290 | controller logic **plus** direct `subprocess` calls |

Known boundary violations:

- `models/node.py` imports `docker`, reads `/proc/*/ns/net`, calls `setns`,
  creates veth/dummy interfaces, and executes container commands. This breaks
  the models boundary in AGENTS.md and makes pure model tests impossible.
- `services/mixins/routing_mgr.py:88-115` hard-codes built-in daemons in an
  `if/elif` chain even though `TopologyManager._daemon_registry` already
  exists; adding a daemon requires editing the factory (OCP violation).
- `routing/p4_daemon.py:3` imports `TopologyManager` at runtime; daemons depend
  on the full manager surface rather than a narrow context (ISP/DIP violation).
- Static import analysis finds a **type-level** cycle:
  `services.topology_satcom` → `services.mixins.routing_mgr` → built-in daemons
  → `routing.base_daemon` → `services.topology_satcom`. The last edge is
  `TYPE_CHECKING`-only; no runtime module cycle was found, but the type
  dependency still ties the layers together. `p4_daemon` also imports the
  manager at runtime.
- `models/node.py` and `models/link.py` import non-model modules; container,
  namespace, link-budget, and utility concerns leak into the model layer.
- `services/mixins/controller.py`, `services/mixins/diagnostics.py`, and
  `utils/node_terminal_bridge.py` call `subprocess` directly; the traffic
  modules receive a node command executor but still contain direct
  `subprocess.run` calls for OS integration.
- `utils/test.py` is a production geodesy module with a test-like name and no
  internal consumer; `utils/coverage.py` is satellite coverage geometry, not
  test coverage; `utils/node_terminal_bridge.py` is a CLI entry point, not a
  generic helper. `utils/satcom_fix.py` applies import-time monkey patches by
  design (documented in `docs/utils.md`), which is a deliberate dependency on
  external library internals rather than an accidental one.

### 2.4 Test-suite debt

- No shared `conftest.py`; `_make_node`, `DummyTopology`, fake label managers,
  and recording nodes are duplicated across suites.
- No `unit`/`integration`/`slow`/`benchmark` markers. The 2,000-node benchmark
  and real-sleep tests run in all five CI matrix legs.
- Many tests assert private attributes, exact shell strings, or patch internal
  methods (`test_static_daemon.py` has 59 `patch` uses), which makes
  behavior-preserving refactors harder.
- Several tests patch a non-existent `ospf_daemon.open` with `create=True`;
  those patches are no-ops.
- Some tests depend on the repository root being the working directory.
- Some assertions are tautological (e.g. `test_budget.py:298`).

### 2.5 Documented contract conflicts to resolve before changing behavior

These are specification decisions, not refactor steps. Each must be resolved
explicitly (fix code, fix docs, or record the current behavior as intended)
before a phase touches it.

| Conflict | Evidence | Resolution |
|---|---|---|
| AGENTS.md forbids Docker/gRPC/filesystem in models, while `docs/models.md` documents `Node.execute_command` and container execution as model behavior | `AGENTS.md` models boundary; `docs/models.md:111-129`; `models/node.py:88-314` | target state: keep `Node` methods as public delegating facades, move implementation behind ports; document the transition |
| `start_gonetem()` documented as `float \| None`, code returns `(container_time, link_time)` | `docs/topology_manager_api.md:10`; `network_lifecycle.py:95` | decide the public contract; if code is wrong, fix in a separate behavior PR |
| `next_step()` documented as `None`, code returns elapsed seconds | `docs/topology_manager_api.md:64`; `simulation_loop.py:92-105` | document the current return value or change it with tests |
| `get_node_IP_addresses` documented, code exposes `get_node_ip_addresses` | `docs/topology_manager_api.md:250`; `diagnostics.py:632` | add a documented alias or fix the docs; adding an alias is a public API change |
| `FlowScheduler.flow_timeout_sec` documented default 180.0, code default `None`, and a test enforces `None` | `docs/traffic.md:357,367`; `flow_scheduler.py:159`; `tests/traffic/test_flow_scheduler_timeout.py:170-178` | choose the contract before Phase 4; tests are the current source of truth |
| `controller_node_iface_pattern` documented as selecting the interface, code hardcodes `eth(50000+id)` and never reads it | `docs/controller.md:18`; `controller.py:288-290` | separate bug/spec decision; do not silently change interface naming in a refactor |
| Routing docs say the manager knows no routing internals, code hard-codes every built-in daemon | `docs/routing.md:21-22`; `routing_mgr.py:10-13,90-106` | Phase 3 aligns code with the documented extension model |
| `P4Daemon` requires a second constructor argument and is absent from the registry; it cannot be registered by the current factory | `p4_daemon.py:71`; `routing_mgr.py:103`; `routing/__init__.py` | confirm a supported use case or deprecate/remove it in a separate approved change |

## 3. Target state

### 3.1 Dependency direction

```text
domain          models, addressing, link_budget
                        ^
                        |
capabilities    routing, traffic, launchers      (implement contracts)
                        ^
                        |
orchestration   services                         (composition root)
                        |
                        v
boundaries      ports/protocols  <-  adapters
                                    (Docker, gRPC, shell,
                                     namespaces, filesystem)

support         utils  (generic helpers; no domain logic, no I/O in
                       modules imported by the domain layer)
```

Rules:

- Domain packages do not import orchestration, adapters, or `utils` modules that
  perform I/O.
- Orchestration depends on explicit protocols for external systems; real
  adapters are injected at composition time, with defaults that preserve the
  current constructor API.
- Capability packages (`routing`, `traffic`, `launchers`) extend existing base
  contracts; new implementations do not require edits to callers.
- `utils` contains only generic, dependency-free helpers.

### 3.2 SOLID mapping

| Principle | Current symptom | Target | Phase |
|---|---|---|---|
| SRP | 1,000+ line modules mixing computation, I/O, parsing | split by capability; pure functions separated from adapters | 3–6 |
| OCP | hard-coded routing daemon `if/elif`; `start_gonetem` constructs `GoNetEmLauncher` directly | registry/factory extension points; new implementation = new class + registration | 2–3, 6 |
| LSP | base launcher returns `None` while concrete returns a tuple; concrete accepts extra kwargs; daemon/base contracts untested | contract tests; align return types/signatures; callers depend only on the ABC | 3, 6 |
| ISP | daemons depend on full `TopologyManager`; mixins share broad state | narrow `RoutingContext`/mixin contracts; explicit collaborators | 2, 5 |
| DIP | direct `docker`, `subprocess`, gRPC, `setns` in domain/orchestration | protocols + adapters; inject fakes in tests | 2, 5, 6 |

### 3.3 Public API compatibility

The refactor preserves documented imports and behavior:

- `TopologyManager`, `NetworkConfig`, model classes, daemon classes, and
  `NetworkLauncher` remain importable from their current paths.
- When a module is split or renamed, the old path becomes a thin re-export
  facade for at least one release cycle.
- No configuration-file or YAML schema changes unless a separate specification
  is approved.
- Generated protobuf modules are not edited by hand.

## 4. How each change is executed

Every phase is delivered as a sequence of small pull requests against `develop`.

### 4.1 Per-PR protocol

1. **Spec** — state the invariant being preserved, the public surface touched,
   the acceptance tests, and what is explicitly out of scope.
2. **Characterization (RED)** — add tests that pin current behavior. For a pure
   refactor, the "red" step is a test that fails because the desired seam does
   not exist yet (e.g. a fake cannot be injected), or a new test on a currently
   untested path that is observed passing against the legacy behavior.
3. **Smallest step (GREEN)** — introduce one seam, move one responsibility, or
   split one module. Behavior must not change.
4. **Refactor** — with tests green, simplify names/duplication introduced by the
   move.
5. **Validate** — targeted tests, full suite, ruff, scoped mypy, coverage, and
   complete diff review.
6. **Report** — commands, exit statuses, tradeoffs, and any behavior that could
   not be exercised locally (Docker/root).

### 4.2 Guardrails

- One concern per PR; no drive-by formatting or renames.
- Mechanical moves are separate commits from logic changes.
- No test is deleted or weakened to accommodate a refactor. If a test asserts a
  private implementation detail, replace it with a behavior test **before** the
  refactor, not after.
- A refactor PR must not change public behavior. Any required behavior change
  gets its own specification, tests, and approval.
- No phase starts until its predecessor's exit criteria are met, unless the
  dependency is explicitly waived in the PR description.
- Keep `develop` green. Revert rather than stack fixes on a red integration
  branch.

### 4.3 Phase specification template

Each phase begins with a short design note (in the first PR or an issue)
containing:

- problem statement and invariants;
- public API impact;
- protocol/interface design and dependency direction;
- acceptance tests and coverage targets;
- migration/rollback strategy;
- explicit non-goals.

## 5. Phases

| Phase | Outcome | Depends on | Risk |
|---|---|---|---|
| 0 | Real quality gates and baseline | — | Low |
| 1 | Shared fakes, deterministic suite, coverage safety net | 0 | Medium |
| 2 | Ports/adapters for Docker, gRPC, shell, namespaces | 1 | Medium–High |
| 3 | Contract-driven, extensible routing daemons | 2 | High |
| 4 | Decomposed traffic execution/parsing/scheduling | 2 | Medium |
| 5 | Documented mixin contracts; reduced manager/Node coupling | 2 | High |
| 6 | Launcher/builder/utils boundaries and packaging | 2 | Medium |
| 7 | Full enforcement, security gates, final validation | 3–6 | Low |

### Phase 0 — Baseline and guardrails

**Objective:** make the quality gates real before production refactoring.

**Work items**

1. Commit [AGENTS.md](../AGENTS.md) and this plan; add a PR template that asks
   for spec, RED evidence, validation commands, tradeoffs, and unverified
   assumptions.
2. Configure pytest: declare `unit`, `integration`, `slow`, `benchmark`
   markers; add `--strict-markers`; document the default fast selection.
3. Configure coverage: source `satgonetem`, omit generated `proto/`, set
   `fail_under = 62` as the initial ratchet, and fail CI below it.
4. Ratchet ruff in measured steps: first `F401`/`F811`/`F841` after a cleanup
   PR, then `I` and `E4`/`E7`; never narrow the existing selection.
5. Add `[tool.mypy]` and a scoped, blocking CI job starting with
   `satgonetem/models`, `satgonetem/addressing`, and
   `satgonetem/link_budget`; fix the current 16 errors in those packages.
   Widen package by package; do not claim whole-repo typing until the job
   covers it.
6. Packaging hygiene: remove `pytest` from runtime dependencies; make
   `pyproject.toml` the single metadata source; delete or regenerate the stale
   `setup.py`, which currently disagrees on name, Python version, and
   dependencies. Verify `uv build` and the wheel smoke test.
7. Enforce repository settings: require PRs and the CI checks on `develop` and
   `main`; make `release.yml` depend on a green lint/test run (or verify the
   tagged commit's CI status).
8. Record the baseline (test count, coverage, ruff rules, mypy scope, runtime)
   in this document's metrics table.
9. Resolve the documentation/code conflicts listed in section 2.5 that can be
   settled without behavior change (fix docs or record current behavior);
   open explicit issues for the ones that require a behavior decision. Each
   Phase 3–6 entry references its resolved decision.

**Exit criteria**

- CI blocks on lint, scoped mypy, tests on 3.10–3.14, coverage `>= 62%`, and
  package build.
- `pytest --strict-markers` passes; markers are documented.
- Release cannot publish from a commit without green CI.
- A documented branch-protection configuration exists for `develop` and `main`.
- Documentation/code conflicts in section 2.5 are resolved or tracked as
  explicit behavior issues.
- No production behavior changed.

**Risk:** the scoped packages currently report 16 mypy errors (293 repo-wide),
so adopting even the domain subset is real work. Mitigation: fix the 16 errors
in the same phase, use `--ignore-missing-imports` for untyped third parties,
track the repo-wide count, and widen only after the current scope is at zero.

---

### Phase 1 — Test architecture and safety net

**Objective:** make refactoring safe by characterizing current behavior and
removing test-suite coupling.

**Work items**

1. Add `tests/conftest.py` and `tests/support/` with shared builders/fakes:
   node/link/topology builders, `DummyTopology`, fake command executor, fake
   gRPC client, fake Docker container/runtime, and repository-root path
   fixtures. Replace the duplicated helpers in the routing, traffic, services,
   and launcher suites.
2. Classify tests with markers. Run fast unit tests on every push; run
   `slow`/`benchmark` in a separate scheduled or manual job. Remove the
   benchmark from the five-leg default matrix.
3. Make tests deterministic and CWD-independent: replace sleeps with
   events/condition polling, use `tmp_path` for outputs, root topology fixtures
   at the repository root, and isolate the iperf port allocator.
4. Add characterization tests for the untested/high-risk seams, in priority
   order: `FlowScheduler` lifecycle/error contract, `p4_daemon`,
   `node_terminal_bridge`, `graph_shared_memory`, `interface_mgr`,
   `link_ops`, `topology_sync`, `traffic_testing`, `controller`,
   `diagnostics`, `bird_service`, `ip_utils`, `utils/coverage`, and the
   uncovered `srmpls`/`isis` paths.
5. Remove test debt: delete the no-op `ospf_daemon.open` patches, fix
   tautological assertions, and replace private-attribute assertions with
   observable behavior where it does not reduce coverage of a real contract.
6. Raise the coverage ratchet to at least 65% globally, with per-module floors
   for every module scheduled for refactoring (75% unless the phase states
   otherwise). **Achieved 2026-11-22:** global coverage is 85.1% and the
   enforced gate is 75%.

**Exit criteria**

- Shared fakes/builders are used by all new tests; no new local copies.
- The default suite passes from any working directory and is free of
  unmarked slow/benchmark tests.
- Coverage `>= 65%`; each Phase 3–6 target module is characterized before its
  refactor starts.
- No production behavior changed.

**Risk:** characterization tests may preserve existing bugs. Mitigation:
characterize only documented behavior and obvious contracts; file defects as
separate issues instead of fixing them silently in a refactor.

---

### Phase 2 — Ports and adapters for external boundaries

**Objective:** apply DIP/ISP to Docker, gRPC, shell execution, filesystem, and
Linux namespace operations.

**Design**

- Define narrow protocols next to their consumers (or in a small `ports.py`
  per package):
  - `CommandExecutor` — run argv, timeout, return stdout/stderr/exit status;
  - `ContainerRuntime` — inspect containers, exec, logs, remove;
  - `GoNetEmClient` — the gRPC operations used by launchers;
  - `NetworkNamespace` — enter/restore namespaces, create veth/dummy
    interfaces, with a real adapter and a fake for tests.
- Provide default adapters so existing constructors continue to work.
- Treat `Node.execute_command` as the de facto command port for traffic and
  routing; keep it as a facade that delegates to the injected executor.
- Migrate consumers in small steps: routing command execution → traffic
  execution → controller/diagnostics → launcher gRPC → HIL `nsenter`/`tc`/`ip`
  → node container and namespace operations.
- Make filesystem assumptions explicit where they are part of an adapter:
  `/tmp/interfaces`, `/tmp/results`, CWD-relative route files, and terminal
  bridge paths. Pure parsing/serialization stays outside the adapter.
- Keep the adapters thin: no business logic, no topology knowledge.

**Exit criteria**

- Targeted orchestration/domain code no longer calls `subprocess`, `docker`,
  or gRPC stubs directly; it depends on the protocols.
- Each migrated consumer has a test that substitutes a fake and asserts the
  same observable behavior as before.
- The abstraction count is justified by at least two real implementations
  (production adapter + test fake); no speculative layers.

**Risk:** namespace operations are process-global and hard to unit test.
Mitigation: isolate them behind `NetworkNamespace`, test argument construction
and error paths with fakes, and validate real behavior with the existing
`scripts/verify_connectivity.py` integration script.

---

### Phase 3 — Routing subsystem

**Objective:** make routing daemons contract-driven and extensible.

**Work items**

1. Add a parametrized `RoutingDaemon` contract suite covering `init`,
   `update`, `remove`, failure handling, idempotence, node-scoped removal,
   worker limits, and the single-argument construction contract used by the
   factory. Run it against every built-in daemon.
2. Replace the hard-coded built-in `if/elif` in `RoutingManagerMixin` with
   registry lookup. Register built-ins the same way custom daemons already are.
   Centralize the built-in name list (it is duplicated in `TopologyManager` and
   `LinkOpsMixin`), and decide whether `allowed_routing_methods` is a live view
   or a construction-time snapshot. Add a test proving a dummy daemon can be
   registered and initialized without editing the factory; add a fixture that
   resets the class-level registry between tests.
3. Introduce a narrow `RoutingContext` protocol for the topology surface
   daemons actually use (nodes, links, command execution, address plan,
   configuration). Remove the runtime `TopologyManager` import from
   `p4_daemon.py`; keep `TYPE_CHECKING` imports only where a real type
   dependency remains.
4. Extract shared helpers for route computation, command generation, config
   templating, installation, and verification. Start with the demonstrated
   duplication: the OSPF/ISIS Bird lifecycle and `_assign_ips`/`_apply_config`
   pair, MPLS module enabling, base64 `ip -batch` execution, and the SR-MPLS
   change-set algorithm. Deduplicate across static, SR-MPLS, ISIS, OSPF, and P4
   without changing generated commands or routing semantics. Introduce a
   `RouteInstaller`/`MplsCommandExecutor` adapter where the Phase 2 executor
   applies.
5. Split `static_daemon.py` and `srmpls_daemon.py` by responsibility:
   computation/planning, command/config generation, execution, and
   parsing/verification. Keep public daemon classes and imports stable.
6. Inventory dead and legacy paths (for example the unreferenced NetworkX
   Dijkstra helpers and legacy NodeRun fallback in `static_daemon.py`) with
   static references plus tests. Deprecate and remove them only in an explicitly
   approved change; do not silently delete public-looking methods.
7. Add `p4_daemon` tests (process lifecycle, timeout, failure cleanup) before
   moving its subprocess calls behind the Phase 2 executor, or de-adopt it per
   the decision above.

**Exit criteria**

- Every daemon passes the contract suite.
- Adding a routing method requires no modification to `RoutingManagerMixin`.
- No package-level import cycle remains: `routing` does not import `services`
  at runtime or under `TYPE_CHECKING` for the manager, and `services` depends on
  the daemon contract, not the reverse.
- Routing coverage `>= 80%`, with `p4_daemon` and `bird_service` no longer at
  0%.
- Documented routing behavior and generated commands are unchanged.
- Before investing in `p4_daemon`, confirm it has a supported use case. If it
  does not, mark it deprecated and remove it in a separate, explicitly approved
  change instead of refactoring dead code.

**Risk:** tests currently assert exact shell strings. Mitigation: move those
assertions to the command-generation layer and keep at least one end-to-end
command fixture per daemon; validate live behavior with the connectivity script.

---

### Phase 4 — Traffic subsystem

**Objective:** separate command building, execution, parsing, result models,
and flow lifecycle.

**Work items**

1. Characterize parser and flow behavior with fixture outputs before moving
   code. Add a corpus for iperf3/hping3/ping/iperf2 output variants.
2. Define a `Flow` protocol (`start`, `stop`, `status`, `results`, `delay`,
   `join`) and a shared threaded-flow base. Migrate `PingFlow` first using
   existing tests plus new stop-during-delay and process-isolation tests, then
   Hping3 and Iperf3. This removes `FlowScheduler`'s reads of `flow._thread` and
   direct `flow.delay` mutation.
3. Split `iperf3_utils.py` into a package: command construction, execution via
   `CommandExecutor`, pure parsing, result/plot models, and flow lifecycle.
   Keep `satgonetem.traffic.iperf3_utils` as a compatibility facade.
4. Apply the same decomposition to `hping3_utils.py`, `ping_utils.py`, and
   `iperf2_utils.py`, extracting shared sampling/flow helpers where the same
   behavior is demonstrably used.
5. Complete `FlowScheduler` contract tests: constructor validation, state
   transitions, `results()`/`errors()` after completion and after failure,
   timeout handling, and `save_results=False`.
6. Replace real sleeps with events and parametrize the repetitive one-flag
   command tests.
7. Move plotting/OS-integration side effects out of parsing and execution
   paths; make them explicit at the facade boundary. Avoid eager pandas/
   matplotlib imports in `traffic/__init__.py`, which currently makes every
   `satgonetem.traffic` import load the plotting stack.
8. Preserve current stop semantics during the flow unification. Ping/Hping3
   currently kill all same-named processes on the source, while iperf3 scopes
   the kill by port; changing that is a behavior fix with its own tests, not a
   refactor by-product.

**Exit criteria**

- No traffic module exceeds ~400 lines except a thin facade.
- Parsers are pure and covered by fixture corpora (`>= 90%` on parser modules).
- Scheduler lifecycle/error contracts are tested; traffic coverage `>= 80%`.
- Existing imports and result shapes are unchanged.

**Risk:** output formats differ across tool versions. Mitigation: fixture
corpora and tolerant parsing tests; do not change parsing semantics in this
phase.

---

### Phase 5 — Services and `TopologyManager`

**Objective:** reduce shared-state coupling and make orchestration testable.

**Work items**

1. Write the mixin contract: required attributes, methods, initialization
   order, lock ownership, and events. Add contract tests using a shared
   harness. Document it in `docs/services.md`.
2. Extract pure topology diff/sync computation from I/O in
   `services/mixins/topology_sync.py`: a normalization helper for the 8-way
   `_build_link_key` type matrix, a `LinkDiffPlan` (add/update/remove), and
   distance batching. Test the matrix for local/satcom satellite and ground
   station combinations; leave `simulation_manager` I/O in the mixin.
3. Separate the simulation loop state machine from thread management; inject
   the stop event/clock where practical and test tick ordering, failure
   propagation, and shutdown deterministically.
4. Move direct `subprocess` calls in `controller.py` and `diagnostics.py`
   behind `CommandExecutor`; add tests for command construction and error
   paths. Extract `NodeTerminal` from diagnostics into its own module and reuse
   it for the bridge instead of maintaining two `NodeExec` stream clients.
5. Introduce a `LinkBackend` protocol for the operations `LinkOpsMixin` needs
   (`update_links`/`add_link`/`update_link`/`delete_link`) and a pure
   `LinkOperationStats` accumulator. Keep HIL routing as a separate
   collaborator. Preserve the current stats semantics; the fact that failed
   operations may still be counted is a behavior question for a separate spec.
6. Make `NetworkConfig` field copying single-source: one normalization/copy
   path shared by the dataclass, `_apply_network_config`, and `to_file`, with a
   round-trip test that fails when a field is added in only one place.
7. Reduce `TopologyManager` responsibilities. Extract collaborators only where
   there is a real second consumer or a clear state boundary; do not replace
   mixins with a new indirection layer wholesale.
8. Address model-layer leakage: separate `Node`'s domain identity/topology
   state from container execution and namespace/veth operations behind the
   Phase 2 protocols, keeping public methods as delegating facades (sub-phase
   5b). Give `Link` the existing `CapacityStrategy` abstraction instead of
   constructing concrete strategies internally.

**Exit criteria**

- Mixin contracts are documented and tested.
- `controller`, `diagnostics`, and `traffic_testing` coverage `>= 70%`;
  `topology_satcom` and mixins do not regress.
- `LinkOpsMixin` depends on `LinkBackend`, not concrete launcher/protobuf
  details; `NodeTerminal` has one implementation used by both consumers.
- `NetworkConfig` has a single field-copy path and a round-trip test.
- Lifecycle, simulation-loop, and sync behavior are unchanged and verified by
  targeted tests plus `scripts/verify_connectivity.py`.
- `Node` no longer owns direct Docker/namespace implementation details; only
  thin delegating methods remain.

**Risk:** lifecycle locking and simulation timing are subtle. Mitigation:
characterization tests for ordering/idempotence, keep locks unchanged until
tests cover them, and require a live integration run before merging 5b.

---

### Phase 6 — Launchers, project builder, utils, packaging

**Objective:** finish boundary cleanup and extension points.

**Work items**

1. Fix the `NetworkLauncher` contract first: align abstract return types and
   signatures with the concrete backend (`start_containers` currently returns
   `None` in the ABC but a tuple in `GoNetEmLauncher`, and the concrete method
   accepts extra `controller`/`links` kwargs). Add a contract/conformance
   suite and a fake launcher. Then make `NetworkLifecycleMixin` obtain the
   launcher from a factory/registry instead of constructing `GoNetEmLauncher`
   directly, so `network_launcher` becomes functional rather than metadata.
2. Split `gonetem_launcher.py` into project/YAML generation, a `GoNetEmClient`
   adapter, and lifecycle orchestration. Move its Docker fallback behind the
   Phase 2 container adapter. Add a conformance test for `HILManager`, including
   its direct `nsenter`/`tc`/`ip` calls.
3. Split `utils/project_builder.py` into configuration models/serialization,
   TLE/CSV ingestion, and sample-project construction. Make filesystem access
   explicit and testable with `tmp_path`.
4. Utils cleanup, with compatibility facades:
   - rename `utils/test.py` to a meaningful geodesy/orbital module;
   - rename `utils/coverage.py` to satellite-coverage terminology, or move it
     to the owning domain package;
   - move `node_terminal_bridge.py` to a CLI package while preserving
     `python -m satgonetem.utils.node_terminal_bridge`;
   - relocate `graph_shared_memory.py` next to its routing/transport consumer;
   - keep the documented `utils/satcom_fix.py` import-time patch behavior, but
     make the patch idempotent and covered by an explicit contract test; if a
     later specification wants to remove the import-time side effect, treat it
     as a behavior change rather than part of this refactor.
5. Enforce packaging single-source: remove the stale `setup.py` or generate it
   from `pyproject.toml`; remove dev-only dependencies from runtime metadata;
   verify the wheel smoke test.

**Exit criteria**

- Launcher contract suite passes for every backend; the ABC return types and
  signatures match implementations, a dummy launcher works without caller
  changes, and `network_launcher` selects the backend.
- `HILManager` passes its conformance tests with external commands behind
  adapters.
- Project builder has no hidden CWD/filesystem assumptions and `>= 75%`
  coverage.
- `utils` contains only generic helpers, with documented compatibility
  exceptions.
- Packaging metadata has one source of truth; wheel install smoke test passes.

**Risk:** module renames break external imports. Mitigation: keep old paths as
re-export facades for one release and document deprecations.

---

### Phase 7 — Enforcement ratchet and final validation

**Objective:** lock in the gains and remove temporary tolerances.

**Work items**

1. Widen mypy to the full `satgonetem` package with a blocking CI job; add
   annotations where required. Do not enable strict mode until the package is
   clean at the current level.
2. Widen ruff to `I`, `B`, `UP`, `SIM`, `E4`, `E7`, and formatting checks
   (choose `ruff format`, not a second formatter). Remove `black`/`flake8` from
   dev dependencies if they remain unused.
3. Raise coverage to the agreed ceiling (target 75–80%) and add a patch/diff
   coverage check for changed lines if the tooling is available.
4. Add security gates: dependency audit (`uv`/`pip-audit`), CodeQL or bandit,
   and action linting. SHA-pin GitHub Actions if the maintenance cost is
   accepted.
5. Verify branch protection requires all gates on `develop` and `main`, and
   that release depends on green CI.
6. Run a final architecture review against AGENTS.md: dependency directions,
   no import cycles, no direct external calls in domain/orchestration code,
   public facades intact. Update README and module docs.
7. Record final metrics, retrospective, and remaining debt. Close the plan or
   open a follow-up for deferred items.

**Exit criteria**

- All gates are blocking: lint, full mypy, tests on 3.10–3.14, coverage,
  security, package build, release.
- Global coverage `>= 75%`; no target module below its phase floor.
- `develop` and `main` require pull requests and status checks.
- Final diff review and architecture review are recorded.

## 6. Sequencing and parallel lanes

```text
Phase 0 -> Phase 1 -> Phase 2 -> Phase 3 (routing)
                                   Phase 4 (traffic)          -> Phase 7
                                   Phase 5 (services)
                                   Phase 6 (launchers/utils)
```

- Phases 0–2 are strictly sequential; they establish gates, tests, and seams.
- Phases 3–6 touch mostly disjoint packages and may run in parallel **only**
  with explicit file ownership:
  - routing lane: `routing/`, `services/mixins/routing_mgr.py`;
  - traffic lane: `traffic/`, `tests/traffic/`;
  - services lane: `services/` (except `routing_mgr.py`), `models/node.py`;
  - launchers/utils lane: `launchers/`, `utils/`, `tests/launchers/`,
    `tests/utils/`.
- `RoutingManagerMixin` belongs to the routing lane; the services lane must not
  edit it concurrently.
- During Phase 6, `services/mixins/network_lifecycle.py` moves to the
  launchers/utils lane once the launcher contract is fixed;
  `services/mixins/link_ops.py` stays with the services lane and depends on the
  launcher contract without changing it in the same PR.
- Public facades and `satgonetem/__init__.py` have a single owner per release
  window to avoid conflicting re-exports.
- Merge order is routing → services → traffic → launchers/utils if conflicts
  appear; rebase and re-run targeted tests after each rebase.

## 7. Metrics and ratchets

| Metric | Baseline | Phase 1 | Phase 3–6 | Phase 7 |
|---|---|---|---|---|
| Global coverage | 62% | 65% | 70% | 75–80% |
| Refactored-module coverage | 0–65% | characterized | 75–90% | maintain |
| Ruff rule families | 4 | +F401/F811/F841, I, E4/E7 | maintain | +B/UP/SIM/format |
| mypy scope | none (16 scoped / 293 total errors) | 3 domain packages | add touched packages | full package |
| Test runtime | 55.9 s | fast lane < 45 s | maintain | maintain |
| Unmarked slow tests | present | 0 | 0 | 0 |
| Direct boundary calls in domain/orchestration | many | inventoried | 0 in migrated code | 0 |
| Largest module | 1,691 lines | measured | ≤ ~400 lines per non-facade module | maintain |
| Branch protection | none | required checks | required checks | required checks + PR rule |

Current (2026-11-22): global coverage 85.1%; `launchers/` at 100%, `utils/` at
100% except one unreachable guard; suite runtime ~17.2 s with coverage.

Rules:

- Thresholds only move up. A PR may not lower a coverage floor or remove a lint
  rule to get green.
- Every phase reports its measured metrics in the phase-closing PR.
- Coverage is a floor, not a target; behavior-focused tests and contract tests
  take precedence over line-count games.

## 8. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Emulation semantics drift (tc/qdisc, routing, timing) | High | characterization tests before moves; live checks with `scripts/verify_connectivity.py`; keep command/config generation fixtures |
| Public API/import breakage | High | compatibility facades, deprecation note, import smoke tests, no config schema changes |
| Tests encode implementation details | Medium | replace with behavior/contract tests before refactor; review test diffs |
| Discovered bugs or doc/code conflicts get "fixed" inside a refactor | High | log them, write a separate spec, and change behavior only with tests and approval |
| Docker/root-only paths unverified in CI | High | isolate behind protocols; explicit "unverified" reporting; manual integration gate for 5b |
| Large mechanical splits create unreviewable diffs | Medium | separate move-only commits; one module per PR; `git diff --color-moved` review |
| Parallel lanes conflict | Medium | explicit file ownership; rebase and re-run targeted tests |
| mypy burn-down stalls | Medium | scoped adoption, per-package targets, track error count; never claim full typing early |
| Coverage ratchet encourages low-value tests | Medium | contract/behavior tests, diff coverage, review test quality |
| Release bypasses CI | High | release workflow depends on green CI; branch protection verified |
| Scope creep into new features | High | phase non-goals; new behavior requires a separate spec/PR |

## 9. Rollback and checkpoints

- Every PR is independently revertible; no long-lived refactor branch.
- Tag checkpoints after Phase 2 and after each subsystem phase
  (`refactor/checkpoint-*`) to give a known-good baseline for integration
  testing.
- Keep facades until the deprecation window ends, so reverting a split does not
  require reverting external imports.
- If a phase cannot meet its exit criteria without behavior change, stop and
  write a separate specification rather than expanding the refactor.

## 10. Governance

- One owner per phase (human or agent session); reviewers check the spec,
  RED evidence, test quality, and full diff.
- A phase closes only when its exit criteria are demonstrated with commands and
  results in the closing PR.
- Waivers are explicit, time-boxed, and recorded in this document.
- Update this plan when a phase's scope changes; do not let the document drift
  from the work.

## 11. Kickoff sequence

The first pull requests, in order, against `develop`:

1. **Docs/policy:** add `AGENTS.md`, this plan, the PR template, and the README
   links. No code change.
2. **Test configuration:** declare markers, add `--strict-markers`, add coverage
   configuration with the 62% floor, and keep the suite green.
3. **CI gates:** add the scoped mypy job, enforce the coverage floor, and make
   release depend on green lint/test. Resolve only the errors inside the scoped
   packages.
4. **Packaging hygiene:** remove `pytest` from runtime dependencies, make
   `pyproject.toml` authoritative, and delete or regenerate `setup.py`; verify
   the wheel smoke test.
5. **Test support layer:** add `tests/conftest.py` and the first shared fakes;
   move topology fixtures off CWD-relative paths.
6. **Phase 1 lanes:** characterize `FlowScheduler`, `p4_daemon`, controller/
   diagnostics, and the low-coverage utils modules in parallel, one test file
   group per lane.

Do not start Phase 2 until the Phase 1 exit criteria (shared fakes,
deterministic suite, coverage `>= 65%`) are demonstrated.

## Appendix A — Initial debt register

| ID | Item | Principle | Phase | Risk |
|---|---|---|---|---|
| D-01 | Stale duplicate `setup.py` metadata | Single source of truth | 0 | Medium |
| D-02 | Narrow ruff rules; mypy/black/flake8 unenforced | Quality gates | 0, 7 | Low |
| D-03 | No coverage threshold; coverage XML only | Quality gates | 0 | Low |
| D-04 | Branch protection does not require checks | CI/CD gates | 0, 7 | High |
| D-05 | Release can publish from a red commit | CI/CD gates | 0 | High |
| D-06 | No shared test fixtures/fakes; duplicated scaffolding | Test architecture | 1 | Medium |
| D-07 | No markers; benchmark/slow tests run in all matrix legs | Test architecture | 1 | Medium |
| D-08 | CWD-relative test data; real sleeps | Determinism | 1 | Medium |
| D-09 | Dead `ospf_daemon.open` patches; tautological assertions | Test quality | 1 | Low |
| D-10 | Direct `subprocess` in controller/diagnostics/daemons/bridge | DIP | 2, 3, 5 | High |
| D-11 | `Node` mixes domain state with Docker/namespace operations | SRP/DIP | 2, 5b | High |
| D-12 | Hard-coded routing daemon factory ignores registry | OCP | 3 | Medium |
| D-13 | Daemons depend on full `TopologyManager` | ISP/DIP | 3 | High |
| D-14 | 1,245/1,320-line daemons mix computation, I/O, parsing | SRP | 3 | High |
| D-15 | `p4_daemon` at 0% coverage | Test safety net | 1, 3 | High |
| D-16 | 1,691-line `iperf3_utils`; parser/execution/plotting mixed | SRP | 4 | Medium |
| D-17 | `FlowScheduler` lifecycle/error contracts untested | Test safety net | 1, 4 | Medium |
| D-18 | Mixins share broad mutable `self` state | ISP/SRP | 5 | High |
| D-19 | `controller`/`diagnostics`/`traffic_testing` low coverage | Test safety net | 1, 5 | High |
| D-20 | `utils/test.py`, `utils/coverage.py`, `node_terminal_bridge` misplaced | SRP/naming | 6 | Low |
| D-21 | `project_builder` mixes config, I/O, TLE/CSV, samples | SRP | 6 | Medium |
| D-22 | `gonetem_launcher` mixes gRPC, YAML generation, lifecycle | SRP | 6 | Medium |
| D-23 | `utils/satcom_fix` import-time compatibility patching | Explicit dependencies | 6 | Medium |
| D-24 | No security/dependency scanning; Actions not SHA-pinned | Supply chain | 7 | Medium |
| D-25 | Full suite runs five times, including benchmark | CI efficiency | 1, 7 | Low |
| D-26 | Type-level cycle services ↔ routing; runtime P4 upward import | Dependency direction | 3 | High |
| D-27 | `NetworkLauncher` ABC and `GoNetEmLauncher` have incompatible returns/signatures | LSP | 6 | High |
| D-28 | `start_gonetem` hard-codes `GoNetEmLauncher`; `network_launcher` is metadata only | OCP | 6 | Medium |
| D-29 | Three duplicated flow state machines; scheduler reads private `_thread`/`delay` | SRP/ISP | 4 | Medium |

Coverage-related items **D-03, D-15, and D-19 are resolved** as of
2026-11-22: the coverage gate is `fail_under = 75`, `routing/p4_daemon.py` is
86%, and `controller`/`diagnostics`/`traffic_testing` are 77%/78%/100%.
| D-30 | `topology_sync.py` mixes 8-way key type switch, sync I/O, and tick pipeline | SRP | 5 | High |
| D-31 | `NetworkConfig` fields copied in three places | Single source of truth | 5 | Medium |
| D-32 | `Link` bypasses the existing `CapacityStrategy` abstraction | DIP | 5 | Medium |
| D-33 | `LinkOpsMixin` depends on protobuf enums and optional launcher methods | DIP/ISP | 5 | Medium |
| D-34 | Docs/code contract conflicts (return types, timeout default, iface pattern) | Specification | 0–4 | Medium |
| D-35 | Dead/legacy routing paths and unused imports | Clean code | 3 | Low |
