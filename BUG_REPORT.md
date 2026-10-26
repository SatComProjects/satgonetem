# SatGoNetEm — Bug Report and Remediation Plan

**Repository:** `/home/disc/j.arias-suarez/Documents/projects/satgonetem`
**Commit:** `01ad0a3` (branch HEAD, working tree clean at review start)
**Date:** 2026-10-26
**Scope:** `satgonetem/` (16,985 LOC) and `tests/` (5,428 LOC)
**Status:** Review complete. Fixed findings are marked inline with their tracking issue.

## Executive summary

The codebase is functionally rich and its unit tests mostly pass, but several
default and documented workflows are broken. The most serious defects are:

1. **Link budgets use the complement of the elevation angle** (`90° - elevation`),
   so overhead satellites are evaluated as horizon links and vice versa.
2. **Static routing never deletes routes** when a destination disappears; stale
   routes keep sending traffic to dead next hops.
3. **`force_close_project()` filters Docker containers by `name=ntm`**, which
   matches every GoNetEm project on the host and can destroy unrelated runs.
4. **A library startup failure calls `sys.exit(1)`**, terminating the user's
   entire Python process instead of raising.
5. **HIL startup filtering is ignored**: HIL ground stations and links are still
   created by GoNetEm, and handovers add the new bridge before deleting the old
   one, which makes HIL unusable after the first handover.
6. **Traffic result handling misreports normal outcomes**: 100 % packet loss is
   raised as a flow error, UDP `iperf3` defaults to 1 Mbps instead of unlimited,
   and UDP receiver byte/out-of-order counters read the wrong JSON fields.
7. **Documented utility APIs crash**: custom connection strategies are never
   registered, custom ground stations lack a domain, and `create_test_project`
   computes `end_date` from wall-clock time instead of `start_date`.

**Baseline test result before changes:**

- `475 passed, 1 skipped, 4 warnings, 1 collection error` (42.65 s)
- Collection fails for `tests/utils/test_project_builder.py` because it imports
  `_find_satellite_by_name`, `_find_ground_station_by_name`, and
  `_find_ground_object_by_name`, none of which exist in `project_builder.py`.
- 66 Python modules import cleanly; the failing paths are runtime/API paths, not
  import-time failures (with one latent protobuf import-order exception noted in
  `LAUNCH-11`).

**Finding counts:** 4 Critical, 41 High, 47 Medium, 32 Low (124 total).
All Critical/High and most Medium findings below are backed by source inspection;
items marked **Runtime** were reproduced with the installed venv, real CLI tools,
or the shipped Docker image.

## Method and severity definitions

- **Static review:** eight subsystem reviews covering every Python file, plus
  tests and documentation for intended behavior.
- **Automated checks:** `pytest`, `ruff --select F,E9,B`, `mypy` (filtered for
  concrete call/attribute errors), wheel build/metadata inspection, module import
  sweep, and targeted numerical reproductions.
- **Runtime checks:** Python venv at `/tmp/opencode/sgnt-venv`, real `iperf3`,
  `iperf`, `ping`, `hping3`, Linux `tc` in a user network namespace, and
  `docker run` against `jariassuarez/sgnt:satellite`.
- **Severity:**
  - **Critical** — wrong emulation on the default/common path, destructive action,
    or termination of the host process.
  - **High** — crash or wrong result on a common configuration/feature.
  - **Medium** — edge case, degraded behavior, leak, or stale state.
  - **Low** — minor robustness, packaging, documentation, or dead-code issue.

---

# 1. Simulation lifecycle and topology synchronization

## CORE-01 — `start()`/`stop()` race can start two loops and hang `stop()`
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/services/mixins/simulation_loop.py:26-40`

`stop()` sets `running=False` and then joins the old thread; `start()` checks only
`get_running()`, clears the shared `_stop_evt`, and starts a new thread. A
concurrent `start()` in the window between `running=False` and the old thread's
exit clears the event the old loop is waiting on. Both loops then run, and the
`stop()` caller waits forever for a loop that will never observe the event.

**Fix:** Serialize `start`/`stop` with a lifecycle lock; join the old thread before
clearing the event or starting a new thread; give each run its own `Event`.
Add a regression test that hammers `start`/`stop` from two threads.

## CORE-02 — Shutdown joins traffic threads without timeout and never stops flows
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/services/mixins/network_lifecycle.py:86-98`, `simulation_loop.py:40`

`stop_gonetem()` calls `stop()` (unbounded join), then joins every active flow
thread without a timeout and without invoking `flow.stop()`. Traffic commands run
without gRPC deadlines, so one stuck command blocks shutdown forever, before
`close_project()` and HIL teardown.

**Fix:** Join with a bounded timeout; call `flow.stop()` for flows still alive;
clear `_active_flows` in `finally`; escalate to `force_close_project()` after a
timeout.

## CORE-03 — API-driven topology mutations race with the simulation loop
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/services/mixins/simulation_loop.py:49,86,126-131,206`

`next_step()`, `move_to_time()`, and `simulate_link_failure()` call
`update_simulation()` from the caller thread with no lock, while `_loop()` calls
the same method every tick. `_sync_links_to_delete()` can delete links another
pass is adding; `current_time_step` and the routing tables become nondeterministic.

**Fix:** Guard `update_simulation()`, `move_to_time()`, and
`simulate_link_failure()` with one `RLock`, or reject/queue API mutations while
the loop thread is running. Document the chosen policy.

## CORE-04 — An exception in the simulation loop silently stops the emulation
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/services/mixins/simulation_loop.py:83-93`

`_loop()` has a `finally` but no `except`. Any gRPC error, link-key error, or race
exits the thread; only `running=False` is recorded. There is no error state and no
retry, and the last partial update remains applied.

**Fix:** Catch `Exception`, store `last_error`, log with traceback, and expose it
through a status API; optionally retry transient gRPC errors with backoff.

## CORE-05 — `force_stop_gonetem()` leaves the simulation loop running
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/services/mixins/network_lifecycle.py:117-148`

The method does not call `self.stop()`, does not cancel flows, and calls the
streaming `close_project()` instead of `force_close_project()`. The background
loop continues mutating topology against a closed project.

**Fix:** Stop/cancel the loop and flows with bounded joins, then call
`launcher.force_close_project()`, clear `launcher`/routing state, and reset flags
in `finally`.

## CORE-06 — Failed startup leaks an open project because the launcher is assigned last
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/services/mixins/network_lifecycle.py:54-64`

`start_containers()`, `wire_links()`, and `hil.wire_links()` run before
`self.launcher = launcher`. If any raises after GoNetEm opened the project,
`stop_gonetem()` sees `launcher is None` and cannot close containers. A retry
opens a second project.

**Fix:** Assign `self.launcher` before starting; wrap startup in
`try/except` that calls `launcher.force_close_project()` and clears state on
failure.

## CORE-07 — `stop_gonetem()` can permanently block restart
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/services/mixins/network_lifecycle.py:106-113`

`GoNetEmLauncher.close_project()` re-raises on `ProjectClose` failure. Because
`set_gonetem_status(False)` runs after it, a failure leaves `gonetem_is_on=True`.
The next `start_gonetem()` hits `if self.get_gonetem_status(): return` and silently
does nothing forever.

**Fix:** Reset state in `finally`, clear `self.launcher`, and re-raise so the
caller knows the emulator is in an unknown state.

## CORE-08 — `init_routing()` makes the override permanent and never tears down the old daemon
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/services/mixins/routing_mgr.py:72-109`

`routing_method` is assigned to `self.routing` permanently, although the docs say
it overrides for that call only. A re-init replaces `self.routing_daemon` without
calling `remove()` on the old daemon. On failure, the failed daemon stays
installed and `routing_initiated` keeps its previous value.

**Fix:** Use a local effective method; call `old_daemon.remove()` before
replacement; assign `routing_daemon`, `routing`, and `routing_initiated` only after
success; reset them on failure.

## CORE-09 — Stale routing daemon survives `stop_gonetem()`
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/services/mixins/network_lifecycle.py:111-113`, `simulation_loop.py:141-142`

`routing_daemon` is not cleared on stop, and
`rebuild_routing_for_current_timestep()` checks only `get_status()`. After a
stop/start cycle it invokes a daemon bound to the previous container generation.

**Fix:** Set `routing_daemon = None` in stop/force-stop (or call
`delete_routing()` first), and require `get_routing_initiated()` in rebuild.

## CORE-10 — User-terminal/PoP links crash every simulation tick
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/services/mixins/topology_sync.py:227-232,441-443`

`_sync_links_to_delete()` calls `_build_link_key()` for every link returned by
`get_all_links()`, but `_build_link_key()` raises `ValueError` for anything that is
not `Satellite`/`GroundStation`. `get_all_links()` includes user-terminal and
ground-object/PoP links; one such link kills `update_simulation()` on the first
tick, and therefore the simulation thread.

**Fix:** Skip unsupported/endpoint-less links in `_sync_links` and
`_sync_links_to_delete`, or extend key creation and link creation to
`UserTerminal`/`PointOfPresence`. Add a UT-link regression test.

## CORE-11 — Empty initial link set prevents all later links from being added
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/services/mixins/topology_sync.py:156-158,208`

When the first sync sees zero links, it returns before `initial_sync = True`.
Later new links are stored with `to_add=False`, so `_add_link_interfaces()` never
runs and no `LinkAdd` is sent. The links exist in Python but not in the emulator.

**Fix:** Set `initial_sync = True` before the zero-link return, or move the flag
update into initialization.

## CORE-12 — Existing links never refresh `satcom_object`, breaking `simulate_link_failure()`
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/services/mixins/topology_sync.py:169-174,204-206`

For an existing key, the worker returns only a boolean. The `Link.satcom_object`
keeps pointing at the object created when the link was first seen. Strategies that
recreate links each tick (the bundled `everything-visible`) make the stored object
stale, so `simulate_link_failure()` calls `remove(x)` with an object no longer in
the simulation manager and raises `ValueError`.

**Fix:** Return the current `sat_com_link` for the update case and assign it (plus
refreshed metadata) in the main-thread loop; alternatively resolve the current
object by endpoints in `simulate_link_failure()`.

## CORE-13 — HIL handover adds the new bridge before deleting the old one
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/services/mixins/topology_sync.py:484-495`

`update_simulation()` runs add/update before delete. `docs/hil_manager.md` requires
delete-then-add. During handover, `HILManager.setup_link()` tries to create
`brhil<id>` while the old bridge still exists; `_create_bridge()` fails with
`EEXIST`, leaves the new veth orphaned, and the later delete phase removes the old
bridge. `link_ops` clears `to_add`, so setup is never retried.

**Fix:** Execute the delete phase before add/update, then update routing/statistics.
Alternatively make HIL setup atomically replace an existing bridge.

## CORE-14 — Parallel interface removal races on `node.interfaces`
**Severity:** Medium · **Confidence:** High (logic) / Medium (timing) · **Location:** `satgonetem/services/mixins/link_ops.py:290-292,314-321`

Workers remove interfaces from both endpoint nodes concurrently without a lock.
If another worker removes an earlier list element while a worker iterates,
`list.remove()`-style scanning can skip an entry; the worker reports success and
the link is deleted from `self.links`, leaving a stale `Interface` forever.

**Fix:** Perform removals sequentially, or collect removals and apply them in one
thread after the pool completes. Protect each node list with a lock if parallelism
is retained.

## CORE-15 — Failed interface setup duplicates interfaces on retry
**Severity:** Medium · **Confidence:** High (Runtime per lane) · **Location:** `satgonetem/services/mixins/link_ops.py:355-363`

`_build_interfaces_from_link()` appends both interfaces to the link and node
lists before `set_ipv4_address()`/container IP sync. A `RuntimeError`/`OSError`
from that step is not caught by the `(AttributeError, KeyError, TypeError)` handler,
so `to_add` stays `True`; the next tick appends a second pair.

**Fix:** Build a temporary pair, extend shared lists only after IP sync succeeds,
or roll back partial interfaces on any exception while keeping `to_add` for retry.

## CORE-16 — Launcher failures are reported as successful link operations
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/services/mixins/link_ops.py:208-217,327-344`, `satgonetem/launchers/gonetem_launcher.py:379-423`

`GoNetEmLauncher.add_link()/update_link()/delete_link()` catch every exception and
return normally. `_execute_link_add()`, bulk counters, and routing updates therefore
treat failed RPCs as success and clear `to_add`/`to_update`/`to_remove`; Python and
the emulator permanently diverge.

**Fix:** Return a success flag or re-raise; clear flags and update routing only
after confirmed success; leave failed operations queued for the next tick.

## CORE-17 — Disabled/inactive links are still wired and routed
**Severity:** High · **Confidence:** High (code) / Medium (static mode) · **Location:** `satgonetem/launchers/gonetem_launcher.py:71,131-144`, `satgonetem/services/topology_satcom.py:880-903`, `topology_sync.py:204-206`

Three independent gaps:
1. `_write_links(tm.links.values())` ignores `is_active`, so inactive links are
   created in the initial project.
2. `get_current_graph()` only assigns weights; it never removes edges marked
   `disabled=True` or whose wrapper link is inactive.
3. `is_active` changes never set `to_update`, and the gRPC link request has no
   active/disabled field.

In `static_ground_station_link_mode` this means out-of-range GSLs remain wired and
routable. In the default topology no disabled edges were present at t=0, but the
condition is created by the documented static mode.

**Fix:** Filter `_write_links` by `is_active`; remove disabled/inactive edges from
the routing graph; represent link state in the emulator (delete/re-add or add an
explicit state field) when it changes.

## CORE-18 — `to_file()` loses controller settings and grows the project name
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/services/topology_satcom.py:346-359,666-668`

The generated `NetworkConfig` omits all six `controller_*` fields, so defaults are
serialized instead of instance values. `_apply_network_config()` appends
`_{int(time.time())}` on every load, so each save/load round trip adds another
suffix to the persisted project name.

**Fix:** Persist every `NetworkConfig` field (or store the original config object),
and strip the generated uniqueness suffix before serialization.

## CORE-19 — Runtime-state save/load silently drops user terminals and PoPs
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/services/topology_satcom.py:220-315,414-496`

Serialization writes `user_terminals`, `points_of_presence`, and
`links["user_terminal"]`, but `_deserialize_runtime_state()` never recreates them.
A topology saved with UTs/PoPs reloads with none of them and no UT links.

**Fix:** Recreate UTs/PoPs and restore `links["user_terminal"]`; raise/report
unresolvable entries instead of dropping them.

## CORE-20 — `controller_node_iface_pattern` is ignored
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/services/mixins/controller.py:197-201,288-290`

`get_management_interface()` hardcodes `"eth" + str(50000 + node_id)`. The
documented format string is never formatted; a user-supplied pattern does nothing,
and the default `eth{node_id}` contradicts the actual interface name.

**Fix:** Use `self.controller_node_iface_pattern.format(node_id=node_id)` in both
controller address setup and controller-link generation, or remove the field and
update the docs.

## CORE-21 — Empty/unreadable link monitoring crashes or aborts all results
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/services/mixins/diagnostics.py:174-184,277-282`

- With no links, `max_workers=min(MAX_WORKERS, 0)=0` and `ThreadPoolExecutor`
  raises `ValueError`.
- `_process_interface_file()` re-raises `OSError`; `monitor_links()` catches only
  `FileNotFoundError` and parse errors, so one permission/I/O error aborts
  collection for every satellite.

**Fix:** Return `[]` for empty link sets (or use `max(1, ...)`), and catch `OSError`
per future with a warning.

## CORE-22 — Flows started before a later `start()` failure are untracked
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/services/mixins/traffic_testing.py:49-72,122-128`

Flows are appended to `_active_flows` only after the whole source/destination loop.
If a later `Thread.start()` raises, earlier flows keep running untracked and
`stop_gonetem()` will not wait for them.

**Fix:** Append each flow immediately after a successful `start()`, or wrap the
loop in `try/finally` that extends `_active_flows` with everything started.

## CORE-23 — `start_gonetem()` return type contradicts its annotation and docs
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/services/mixins/network_lifecycle.py:18,67`

The method is annotated `-> float | None` and documented as returning elapsed
seconds, but it returns the launcher's `(container_time, link_time)` tuple. Callers
following the docs get a tuple where they expect a number.

**Fix:** Return total elapsed wall-clock time, or update the annotation and docs to
the tuple and document its two event-span semantics.

---

# 2. Link budget and link model

## LINK-01 — Elevation angle is inverted before the link budget
**Status:** Fixed — see issue #18.
**Severity:** Critical · **Confidence:** High (Runtime) · **Location:** `satgonetem/models/link.py:95-107`

`get_elevation_angle()` already returns the elevation above the horizon
(`geometry.py:48-51`; tests assert 90° overhead and −90° at the antipode). The
caller subtracts it from 90:

```python
central_angle = get_elevation_angle(...)
return 90.0 - central_angle
```

Runtime check: a satellite directly overhead returns geometry elevation `90.0`,
and `Link` passes `0.0` to the atmospheric model. At 0° elevation, the ITU slant
path is at best extreme and the service commonly returns 0 kbps; low-elevation
links are treated as near-zenith. Satellites below the horizon yield values >90°,
which the propagation wrapper rejects.

**Impact:** Every GSL with `use_budget=True` receives the wrong atmospheric
attenuation and a wrong capacity — the headline link-budget feature is inverted.

**Fix:** Return `get_elevation_angle(...)` directly, rename the local to
`elevation`, and add a regression test asserting an overhead pass-through.

## LINK-02 — Budget failure returns 0 instead of the documented default capacity
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/models/link.py:179,196`

`LinkBudgetService.compute_one_way()` returns 0 when `itur` is missing, EIRP/G/T
fails, or the strategy raises. `_compute_budget_capacity()` falls back to
`default_capacity_kbps` only when an antenna is missing. Docs
(`docs/link_budget.md:319-325`) promise a fallback for missing `itur` and
computation failures.

**Impact:** A normal install without the optional `itur` extra gets 0 kbps for
every GSL; the initial `network.yml` contains `rate: 0`.

**Fix:** Capture both capacities and substitute `default_capacity_kbps` when the
service returns 0 (or make the service distinguish failure from legitimate zero).

## LINK-03 — Capacities are frozen after antenna attachment and during the pass
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/models/link.py:69,209-233`, `satgonetem/services/topology_satcom.py:754-765`, `topology_sync.py:204-206`

`Link.__init__()` computes capacities before antennas exist, so it falls back to
static defaults. `set_antenna()` only assigns antennas; `set_link_budget_config()`
recomputes but does not set `to_update`; the per-tick sync calls only
`sync_distance_from_satcom_and_delay()`, which never recomputes capacity. The
documented order (`set_link_budget_config` → `set_antenna`) therefore leaves every
GSL at its default capacity.

**Impact:** `use_budget=True` silently produces static-rate GSLs, and even a manual
recompute is not pushed to the emulator until some unrelated delay change.

**Fix:** Recompute affected GSL capacities at the end of `set_antenna()`; recompute
in the per-tick update when distance changes; set `to_update=True` when capacities
change.

## LINK-04 — `compute_one_way_shannon_capacity()` discards the antenna gain
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/link_budget/budget.py:151-168`

`ant.calculate_gain_db(frequency_ghz)` returns the gain but the result is discarded;
the code then reads `ant.gain_db`, which is `None` for an aperture antenna created
from `AntennaConfig`. Runtime reproduction:

```
TypeError: unsupported operand type(s) for +: 'float' and 'NoneType'
```

The RX mirror has the same defect (`None - float`). Tests hide it because their
mocks set `gain_db` explicitly.

**Fix:** `gdb = ant.calculate_gain_db(...)` with a `getattr(..., None) or 0.0`
fallback; do the same for RX.

## LINK-05 — `compute_one_way_shannon_capacity()` ignores a pre-computed EIRP
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/link_budget/budget.py:156-159`

The method rebuilds EIRP from `sspa + gain - losses` and never reads `eirp_db` or
calls `get_eirp_db()`, although the docs say a pre-computed EIRP overrides the
component formula. An antenna with `eirp_db=50` produced 30 dBW, changing a
representative capacity from ~587 Mbps to ~66 Mbps.

**Fix:** Prefer `ant.get_eirp_db(frequency_ghz)` when available, falling back to
the component formula only for objects without that method.

## LINK-06 — `compute_downlink_modcod()` refuses to derive EIRP
**Severity:** Medium · **Confidence:** Medium · **Location:** `satgonetem/link_budget/budget.py:242-244`

It reads `sat_node.antenna.eirp_db` and raises if `None`, even though
`Antenna.get_eirp_db()` derives it from SSPA power + gain − losses and the docs say
derivation is supported. The shipped `test_missing_eirp_raises` codifies the
raise, so either the test or the docs/code must change.

**Fix:** Call `get_eirp_db()` when the antenna exists; raise only when there is no
antenna or derivation fails.

## LINK-07 — Receive antenna losses are never applied to G/T
**Severity:** Medium · **Confidence:** Medium · **Location:** `satgonetem/link_budget/service.py:112-113`, `budget.py:167-168,251-252`

`losses_db` is documented as “Transmit / receive losses”, but RX paths compute
`gain - 10log10(Tsys)` without subtracting it. A 2 dB receive loss yields a G/T
2 dB too high, biasing MODCOD and Shannon results.

**Fix:** Subtract `rx_antenna.losses_db` before forming G/T in all three paths.

## LINK-08 — Link-budget configuration accepts unphysical values
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/link_budget/config.py:23-26,37-42`

No `__post_init__` validation. `downlink_freq_ghz=0` raises `ZeroDivisionError`
inside `Antenna.calculate_gain_db()` (the service catches only
`ValueError`/`TypeError`); `bandwidth_hz=0` reaches `log10(0)`/`linear_to_db(0)`;
negative frequencies and efficiency outside 0…1 silently propagate.

**Fix:** Validate positive frequencies/bandwidth/diameter, `0 <= efficiency <= 1`,
and non-negative losses at dataclass construction; optionally catch
`ZeroDivisionError` as a defensive guard.

## LINK-09 — Missing antennas raise `AttributeError` despite the documented 0 result
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/link_budget/service.py:99-108,111-116`

The docstring promises 0 “when an antenna is missing”, but `None.get_eirp_db()`
raises `AttributeError`, which is not in the `(ValueError, TypeError)` handlers.

**Fix:** Return 0 explicitly when either antenna is `None`, or catch
`AttributeError`.

## LINK-10 — `get_capacity()` and the initial project use downlink capacity for both directions
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/models/link.py:259-265`, `satgonetem/launchers/gonetem_launcher.py:133-141`

`get_capacity()` always returns `peer1_capacity` (downlink), and `_write_links()`
uses that single value for both `peer1qos` and `peer2qos`. For a GND→SAT oriented
link it also applies the downlink rate to the source (ground) side. Incremental
`_link_request()` correctly uses both capacities, so startup and updates disagree.

**Fix:** Use `peer1_capacity`/`peer2_capacity` for the two QoS blocks; normalize
endpoint orientation so SAT egress always gets downlink and GND egress uplink.

## LINK-11 — Link distance is only updated when the integer delay changes
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/models/link.py:228-233`

Delay is integer milliseconds (~300 km per tick), so small orbital movement leaves
`self.distance` stale. Any later budget recompute or diagnostics uses a stale slant
range.

**Fix:** Always assign `self.distance = new_distance`; keep only the delay change
and `to_update` inside the conditional.

## LINK-12 — `distance_3d_km()` is not a 3D distance
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/utils/utils.py:196-206`

The function combines a Haversine surface arc with the vertical difference:

`sqrt((R·θ)² + Δalt²)`

The true geodetic chord is `sqrt(R1² + R2² − 2R1R2cosθ)`. For a 10° separation at
550 km, the code returns 1240.5 km versus ~1284 km true; long ISLs are off by
hundreds of km, affecting delay and FSL. The Haversine `a` is also not clamped, so
near-antipodal floating-point cases can raise `ValueError` (lane reproduced this;
20k random cases did not).

**Fix:** Convert both points to ECEF and return the norm of the difference; if
retaining Haversine, clamp `a` to `[0, 1]`.

## LINK-13 — `hash_node()` crashes on a node with a skipped/incomplete position
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/models/node.py:405-417`

`sync_position_from_satcom()` deliberately skips when coordinates are `None`,
leaving `position == {}`. `hash_node()` indexes `position["latitude"]` and raises
`KeyError` during `check_for_updates()` → `_compute_satellites_hash()`.

**Fix:** Use `position.get("latitude", 0.0)` etc., or skip incomplete nodes in the
hash builders with a warning.

## LINK-14 — `Interface.peer` is annotation-only
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/models/interface.py:13,70`

`self.peer: Interface` creates no attribute. `set_ipv4_address()` on an interface
not built by the topology manager raises `AttributeError`;
`Node.remove_interface_connected_to_node()` catches that and silently fails to
remove the interface.

**Fix:** Initialize `self.peer: Interface | None = None`; raise a clear `ValueError`
when `set_ipv4_address()` has no peer; guard peer access during removal.

## LINK-15 — `GroundStation.satcom_object` is annotation-only
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/models/ground_station.py:48,62`

The `is None` guard itself raises `AttributeError` because the attribute was never
created, violating the documented `ValueError` contract.

**Fix:** Initialize `self.satcom_object: SatComGroundStation | None = None`.

## LINK-16 — QoS mixin IndexError, negative sizes, and division by zero
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/models/mixins/qos_mixin.py:71,84,104,127`

- `lines[1]` raises `IndexError` when `tc qdisc show` returns fewer than two lines.
- `required_limit = total_excess - tbf_burst_int` and
  `required_burst = total_excess - required_limit` can go negative.
- The f-strings interpolate floats (`limit {required_limit}b`), which `tc` generally
  rejects.
- `duration = num_of_bytes / rate_bytes` divides by zero when demand is 0.

The API has no callers in the repository, but it is a public mixin.

**Fix:** Check list length/scan all lines; parse sizes with suffixes (`15Kb`), not
just raw `\d+b`; clamp sizes to non-negative integers; validate non-zero demand;
skip the change when no adjustment is needed.

## LINK-17 — Atmospheric wrapper accepts 0° elevation and 0 % unavailability
**Severity:** Low · **Confidence:** Medium · **Location:** `satgonetem/link_budget/propagation.py:61-72`

ITU-Rpy requires `0.001 < p < 50` and positive elevation; at 0° the slant path is
infinite, and `log10(0)` appears in scintillation. The service checks exceptions,
not `inf`, so LINK-01 can silently produce 0 capacity.

**Fix:** Validate the ITU ranges (`0.001 < unavailability <= 50`, `0 < elevation <= 90`)
or clamp/document the limits explicitly.

---

# 3. Launchers, GoNetEm, and HIL

## LAUNCH-01 — Startup failure calls `sys.exit(1)` inside a library
**Severity:** Critical · **Confidence:** High · **Location:** `satgonetem/launchers/gonetem_launcher.py:233-237,278-280`

`SystemExit` bypasses every `except Exception` handler and terminates the whole
host process. It also leaves a partially opened project/containers and an unclosed
channel; the next start can fail on the occupied project name.

**Fix:** Raise `RuntimeError`; wrap `ProjectOpen`/`TopologyRun`/attach in
`try/except/finally`; on failure best-effort `ProjectClose`, close the channel,
remove the `.gnet` file, and re-raise.

## LAUNCH-02 — `ProjectOpen` response status is ignored
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/launchers/gonetem_launcher.py:242`

`PrjOpenResponse` contains `status` and `id`; the code reads only `.id`. A duplicate
project name is reported through `status.code=ERROR` with a nil gRPC error, so the
client proceeds with an empty id, `TopologyRun` fails, and LAUNCH-01 exits the
process.

**Fix:** Check `resp.status.code == StatusCode.ERROR` and raise with
`resp.status.error`; apply the same check to `ServerGetVersion`.

## LAUNCH-03 — `force_close_project()` can stop every project on the host
**Severity:** Critical · **Confidence:** High · **Location:** `satgonetem/launchers/gonetem_launcher.py:431-461`

`docker ps --filter name=ntm` matches any container whose name contains `ntm`,
i.e. every GoNetEm project (`ntm{prjID}.{node}`) and unrelated names. The method
then stops and removes all matches without checking return codes.

**Fix:** Scope the filter to `^ntm{re.escape(project_id)}\.` (or list containers and
match `startswith`), and check Docker results/errors.

## LAUNCH-04 — Ground stations are launched as `docker.satellite`
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/launchers/gonetem_launcher.py:62-64,123-127`

`gs_type = "docker.host" if fallback else "docker.satellite"  # Borken if using gndstation TODO fix`.
The shipped config defines `docker.gndstation` with the ground-station image, but
`gs.image` is empty (`node.py:65`) so GoNetEm uses the satellite image. Every
non-HIL ground station runs the wrong image.

**Fix:** Use `docker.gndstation` and default `image` to
`jariassuarez/sgnt:ground-station` when `gs.image` is unset.

## LAUNCH-05 — HIL filtering is ignored; GoNetEm still creates HIL nodes and links
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/launchers/gonetem_launcher.py:66-71,239`, `services/mixins/network_lifecycle.py:41-57`

`start_gonetem()` computes `launch_nodes`/`launch_links`, but `generate_project()`
reads the full `tm.satellites`, `tm.ground_stations`, and `tm.links`; the `nodes`
argument is used only for `_attach_containers()`, and `wire_links()` is a no-op.
GoNetEm creates the HIL ground-station container and the `eth{gnd_id}` veth, then
`HILManager.setup_link()` tries to create the same veth and fails with `EEXIST`.

**Fix:** Build the project from the filtered node/link sets (including
`is_active`), or pass `launch_links` into `generate_project()` and filter both
satellites and ground stations.

## LAUNCH-06 — `NetworkConfig.satellite_image` never reaches the launcher
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/launchers/gonetem_launcher.py:118`, `services/topology_satcom.py:675`

`Node.image` is initialized to `""` and never assigned from the config; the launcher
hardcodes `jariassuarez/sgnt:satellite` whenever `sat.image` is empty. A configured
image is silently ignored.

**Fix:** Pass `tm.satellite_image` as the fallback into `_write_satellites()`, or
assign `node.image` during sync.

## LAUNCH-07 — gRPC calls have no deadlines
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/launchers/gonetem_launcher.py:227,242,250,355,386,400,416`

No unary call or server stream has a timeout. `LinkUpdate` runs in the simulation
thread, so a hung server wedges the simulation; `ProjectClose` can hang shutdown.

**Fix:** Add finite unary timeouts (10–30 s) and an overall/per-message deadline for
`TopologyRun`/`ProjectClose`; catch `grpc.RpcError`/`FutureTimeoutError` and clean up.

## LAUNCH-08 — Docker-client failure skips gRPC attachment for all nodes
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/launchers/gonetem_launcher.py:305-309,331-333`

The early `return` on `docker.from_env()` failure skips the loop that assigns
`node.grpc_client` and `node.project_id`, although those are independent of Docker.
A remote GoNetEm server with no local Docker socket leaves every node without gRPC
configuration.

**Fix:** Attach gRPC/project id before the Docker lookup; treat container attachment
as best-effort.

## LAUNCH-09 — Per-start files and Docker clients leak
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/launchers/gonetem_launcher.py:155-159,306`, `routing/ospf_daemon.py:183-185`, `routing/isis_sr_bird_daemon.py:259-261`

The `.gnet` archive is never unlinked, the Docker client is never closed, and every
OSPF/ISIS update writes a fresh UUID `bird_*.conf` under the shared `/tmp` without
deleting it.

**Fix:** Unlink the archive after reading and Bird configs after use (in `finally`);
close the Docker client with a context manager.

## LAUNCH-10 — Generated gRPC stubs require grpcio >= 1.80, dependencies allow >= 1.62
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/proto/netem_pb2_grpc.py:8-25`, `pyproject.toml:28`, `setup.py:19`

`netem_pb2_grpc.py` raises at import on grpcio < 1.80. Both package files declare
`grpcio>=1.62.0`, so a valid resolution can make `gonetem_launcher` unimportable.

**Fix:** Raise the requirement to `grpcio>=1.80.0` or regenerate stubs with the
oldest supported toolchain.

## LAUNCH-11 — Direct import of `netem_pb2` fails in a clean process
**Severity:** Low · **Confidence:** High (Runtime) · **Location:** `satgonetem/proto/netem_pb2.py:27`, workaround at `utils/node_terminal_bridge.py:29`

`netem_pb2.py` depends on `google/protobuf/empty.proto` but does not import
`empty_pb2`; `from satgonetem.proto import netem_pb2` raises
`TypeError: Couldn't build proto file into descriptor pool`. Importing
`netem_pb2_grpc` first (which imports `empty_pb2`) masks it. The terminal bridge
contains an explicit import-order workaround comment.

**Fix:** Add `from google.protobuf import empty_pb2` before the descriptor build
(or regenerate the file with the dependency import).

## LAUNCH-12 — `NetworkLauncher` contract does not match the implementation/caller
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/launchers/base_launcher.py:36-41`, `gonetem_launcher.py:206-212`, `network_lifecycle.py:54-56`

The abstract method has no `controller` parameter and returns `None`; the concrete
class accepts `controller` and returns `(container_time, link_time)`, which
`start_gonetem()` unpacks. A new launcher written to the base contract raises
`TypeError`.

**Fix:** Add `controller: bool = False` to the base and annotate
`tuple[float, float] | None`, or remove `controller` from the caller and wire links
in `wire_links()`.

## LAUNCH-13 — HIL setup has no rollback after partial failure
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/launchers/hil_manager.py:412-415,283-284`

If `_create_bridge()` fails after `_create_veth()` succeeded, the SAT veth remains;
the tracking dictionaries are only updated on full success, so `teardown_all()`
cannot clean it. Every retry then hits `EEXIST`.

**Fix:** Make setup transactional: on any failure delete the SAT veth and the
partial bridge, release the hardware interface, and record failures.

## LAUNCH-14 — HIL teardown forgets a bridge before confirming deletion
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/launchers/hil_manager.py:342-355`

`_teardown_bridge()` pops `_hil_bridges`/`_hil_veths` before the netlink delete.
If deletion fails, the bridge (possibly with the physical interface enslaved)
remains but is untracked, so no retry is possible.

**Fix:** Delete first; pop the tracking entries only after success (or restore them
in the exception path).

## LAUNCH-15 — HIL host interface names are not project-scoped
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/launchers/hil_manager.py:199,267`

`hil{gnd_id}` and `brhil{gnd_id}` depend only on the station id. Two projects with
`Gnd0` collide; tearing down one can delete the other's bridge/enslaved hardware.

**Fix:** Include a short project hash/prefix in the names (respecting the 15-char
interface limit).

## LAUNCH-16 — HIL shell-outs have no timeout and leak `OSError`
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/launchers/hil_manager.py:49-68,318-328,357-375`

`_run_tc_batch()` and `_delete_sat_veth()` call `subprocess.run` without a timeout.
`_apply_qos()` catches only `RuntimeError`, so a missing `nsenter`/`tc` or permission
error propagates out of startup. `setns` return values at lines 224/249 are ignored.

**Fix:** Add timeouts, catch `TimeoutExpired`/`OSError`, check `setns` results,
and roll back partial qdiscs on batch failure.

## LAUNCH-17 — `HILManager.teardown_link()` can leak the host veth
**Severity:** Low · **Confidence:** Medium · **Location:** `satgonetem/launchers/hil_manager.py:437-447`

The bridge is deleted and trackers popped before checking `sat_pid`. If the PID is
missing while the container is alive, the SAT veth and its host peer remain
untracked.

**Fix:** Resolve the PID from the container attrs/name before skipping, or delete
the host veth directly.

## LAUNCH-18 — `set_link_capacities()` never changes existing link rates
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/launchers/gonetem_launcher.py:191-202,425-429`

`_link_capacities_for()` uses `p1 or fb or default`. `peer1_capacity` is always
non-zero after `Link.__init__`, so the launcher's newly assigned defaults are never
selected; `update_link()` resends the old rates.

**Fix:** In `set_link_capacities()`, update non-budget links'
`peer1_capacity`/`peer2_capacity`/`default_capacity_kbps` before calling
`update_link()`, while preserving budget-derived values.

---

# 4. Routing daemons

## ROUTE-01 — Static routing never deletes routes that disappear
**Severity:** Critical · **Confidence:** High (Runtime per lane) · **Location:** `satgonetem/routing/static_daemon.py:651-665`

`_emit_batch_commands()` returns only `curr - prev` as `ip route replace` lines.
There is no `prev - curr` branch and no `route del` anywhere in `update()`/`init()`.
When a destination becomes unreachable, the old kernel route remains and traffic
is sent to a dead next hop. `remove()` only deletes entries present in the current
table, so it cannot clean stale routes either.

**Fix:** Compute `removed = prev.keys() - curr.keys()`, emit
`route del <dest><prefix> via <old_gateway> dev <old_ifname>` before the replaces,
and commit the previous table only after a successful batch.

## ROUTE-02 — Static `init()` reports success when every route installation fails
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/routing/static_daemon.py:55-73,132-171`

`_apply_satellite_routes()`/`_apply_ground_station_routes()` count errors, log them,
and return `None`; `init()` unconditionally returns `True`. Worse,
`_populate_routing_tables()` copies the desired table into `ipv4_previous_routing_table`
regardless of failure, so failed routes are treated as installed and never retried.

**Fix:** Return an error count from the apply methods; `init()` returns
`errors == 0`; update `ipv4_previous_routing_table` per node only after that node's
batch succeeds.

## ROUTE-03 — Static `init()`/`update()` let population exceptions escape
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/routing/static_daemon.py:55,81`

`_populate_routing_tables()` is outside `init()`'s `try`, and `update()` has no
error handling. A missing route file, unresolved interface, or missing graph node
raises through the documented `bool`/`None` contract into the simulation loop.

**Fix:** Move population inside the `try`; wrap `update()` population/apply in the
same error contract and return/report failure.

## ROUTE-04 — SR-MPLS incremental updates never delete stale LFIB/IP routes
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/routing/srmpls_daemon.py:911-919,966-974,1021-1029`

Only lines absent from `prev` are sent, with no deletions; the `prev` cache is
replaced by the new list (or not updated when empty). A removed neighbor's SID
route remains. If the kernel later removes the interface/route, the stale cache
still claims the line is installed, so a returning link is never re-installed.

**Fix:** Diff both directions and emit `ip -f mpls route del <label>` /
`ip route del <dest>` for removed lines; always commit the new cache after a
successful batch, including when it is empty.

## ROUTE-05 — New interfaces never get MPLS input enabled
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/routing/srmpls_daemon.py:397,769-770,787-793`

`_enable_mpls_on_all_nodes()` runs only when `init_flag=True`. Links added later
get kernel-default `net.mpls.conf.<iface>.input=0`; MPLS packets on the new
interface are dropped even though routes point at it.

**Fix:** Enable MPLS input for all current interfaces on every update (idempotent),
or explicitly enable both endpoints of each newly added link.

## ROUTE-06 — `SRMPLSDaemon.remove()` does not flush MPLS or IPv4-encap routes
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/routing/srmpls_daemon.py:1159-1161,907-1029`

The command `ip -f mpls route flush` is invalid; local reproduction prints
`"ip route flush" requires arguments`. The `2>/dev/null; true` hides the failure.
The method also never deletes the `ip route ... encap mpls` and `/32` routes it
installed. Switching routing methods leaves more-specific SR routes that override
static routes.

**Fix:** Use `ip -f mpls route flush table all` and check its status; track and
delete the IPv4/encap prefixes installed by the daemon.

## ROUTE-07 — Per-source SR rebuild drops all satellite destinations
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/routing/srmpls_daemon.py:699,1085`

`_build_sr_gs_routes()` builds routes to `gs_array + sat_array`;
`_update_sr_route_for_source()` rebuilds `sr_lfib[source_id]` using only
`gs_array`. After `set_sr_custom_path()`/`clear_sr_custom_path()`, all routes from
that source to addressable satellites vanish from the in-memory LFIB.

**Fix:** Use the same destination set in both methods (or one shared builder).

## ROUTE-08 — `;`-joined SR batches return only the last command's status
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/routing/srmpls_daemon.py:916-919,971-974,1026-1029,1142-1146`

POSIX `sh` returns the status of the last command in `cmd1; cmd2; ...`; an early
failure is invisible, and the daemon records the whole list as installed.

**Fix:** Use `set -e`/`&&`, or feed commands to `ip -force -batch -` and check the
single exit status.

## ROUTE-09 — `SRMPLSDaemon.init()` reports success despite failures
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/routing/srmpls_daemon.py:379-384,820-825,920-933`

`_enable_mpls_on_all_nodes()` and all three apply methods catch per-node/worker
exceptions and only log; `init()` returns `True` unconditionally.

**Fix:** Return error counts and have `init()` return `errors == 0`, matching the
OSPF/ISIS daemons' contract.

## ROUTE-10 — IS-IS Bird configuration is invalid for the shipped Bird 2.0.7
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/routing/isis_sr_bird_daemon.py:197-218`

The template emits `protocol mpls { ... }` and `protocol isis isis1 { ... }`.
Stock Bird has neither MPLS nor IS-IS protocols. Runtime check in
`jariassuarez/sgnt:satellite` (Bird 2.0.7) rejects `protocol mpls` with
`unexpected MPLS`. Unless a private patched Bird fork is always deployed,
`dynamic-isis` can never load.

**Fix:** Target FRR syntax (as the README claims) or ship/declare the exact patched
Bird fork and validate generated configs against the shipped image in CI.

## ROUTE-11 — OSPF/IS-IS call a helper absent from the satellite image
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/routing/ospf_daemon.py:186-188`, `isis_sr_bird_daemon.py:262-264`

Both daemons run `/usr/bin/start-service.py`. Runtime check: the default
`jariassuarez/sgnt:satellite` image has no such file (exit 127). The ground-station
image does. `OSPFDaemon.init()`/`ISISBirdSRDaemon.init()` therefore fail on every
satellite in the default deployment.

**Fix:** Run `bird`/`birdc` directly with the config path, or add the helper to the
satellite image; also wire `satellite_image` into `Node.image` (LAUNCH-06).

## ROUTE-12 — `hostname` directive is rejected by the shipped Bird
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/routing/ospf_daemon.py:137`, `isis_sr_bird_daemon.py:183`

Runtime check in the shipped image: `bird -p` rejects
`hostname "Sat0";` with `syntax error, unexpected CF_SYM_UNDEFINED`. Removing the
line makes the OSPF config parse.

**Fix:** Remove `hostname` from both templates (it is cosmetic) or gate on the
actual Bird version.

## ROUTE-13 — `start-service.py` masks Bird failures
**Severity:** Medium · **Confidence:** High (Runtime per lane) · **Location:** `satgonetem/routing/ospf_daemon.py:186-188`, `isis_sr_bird_daemon.py:262-264`

The helper's `start_bird()` catches exceptions and returns `False`, but `main()`
exits 0. The daemons only check the process exit code, so they report success while
Bird is not running.

**Fix:** After configuration, verify Bird liveness/loaded config (`birdc show status`
or `birdc configure` exit code); make the helper exit non-zero on failure.

## ROUTE-14 — Custom SR paths are not adjacency-validated
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/routing/srmpls_daemon.py:447-463,1101-1104`

Only node existence is checked. If a consecutive pair is not directly connected,
`_update_sr_route_for_source()` silently `continue`s, leaves the old route in the
kernel, and still returns `{"status": "success"}`.

**Fix:** Validate each consecutive pair against the current graph/interfaces and
raise `ValueError` before mutating state.

## ROUTE-15 — `remove(node=...)` resets global SR state
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/routing/srmpls_daemon.py:410-420`

After flushing one node, the method clears `sr_lfib`, `_sr_graph`,
`_label_manager`, and all previous caches. The next `update()` returns early
because `_label_manager is None`; the remaining nodes never receive updates.

**Fix:** Reset global state only when `node is None`; for a single node, update only
its per-node caches.

## ROUTE-16 — SR-MPLS crashes with zero workers on degenerate topologies
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/routing/srmpls_daemon.py:842,946-950,1001-1005`

`workers = min(32, ..., len(collection))` is 0 when there are no satellites or no
ground stations; `ThreadPoolExecutor(max_workers=0)` raises `ValueError`.

**Fix:** Return early for empty collections or use `max(1, ...)`.

## ROUTE-17 — IS-IS Node SID can fall outside the configured SRGB
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/routing/isis_sr_bird_daemon.py:24-25,213-216`

SRGB is 16000–23999 (8000 labels) but the index is the raw `node.id`, which can be
up to 16383; ids ≥ 8000 produce labels outside the SRGB.

**Fix:** Validate the index range or allocate a within-SRGB index.

---

# 5. Traffic tools

## TRAFFIC-01 — Normal 100 %-loss ping runs become flow errors
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/traffic/ping_utils.py:342`, `satgonetem/models/node.py:120-123,290-295`

`ping` exits 1 when no replies arrive, and `Node.execute_command()` raises on any
non-zero exit before `PingResults` can parse the perfectly valid summary. Every
unreachable destination becomes `PingStatus.ERROR`; `test_connectivity()` reports
zero lost packets and counts the pair as an error.

**Fix:** Allow exit code 1/2 for ping (or append `; true`), and let `PingResults`
validate the output. Add an unreachable-destination regression test.

## TRAFFIC-02 — Normal 100 %-loss hping3 runs error, and stderr is discarded
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/traffic/hping3_utils.py:115,177-184,446`

hping3 exits 1 with no replies, so `execute_command()` raises before parsing.
Separately, the `--- hping statistic ---` summary is written to stderr; the gRPC
`NodeRun` path returns only stdout (`node.py:290`), so packet counts/loss are
always `None` on a normal started topology. The tests mock combined output and hide
both issues.

**Fix:** Accept hping3 exit code 1, append `2>&1`, and return/expose stderr from
`Node.execute_command()`.

## TRAFFIC-03 — Ping statistics regex rejects duplicate replies
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/traffic/ping_utils.py:58-62`

The optional group allows only `errors`, not `duplicates`; iputils emits
`+1 duplicates` and the regex fails, turning a successful run into
`ValueError("Could not parse ping statistics")`.

**Fix:** Accept repeated `+N (errors|duplicates)` groups; optionally support BusyBox
wording.

## TRAFFIC-04 — Ping configuration accepts hanging/zero values
**Severity:** Medium · **Confidence:** High (Runtime) · **Location:** `satgonetem/traffic/ping_utils.py:31-49`

`timeout_sec=0` becomes `-W 0`, which disables the per-reply timeout and can hang
the flow; `count < 1` and negative packet sizes are accepted and fail only inside
the tool.

**Fix:** Require `timeout_sec > 0`, `count >= 1`, `packet_size >= 0` in
`__post_init__`.

## TRAFFIC-05 — hping3 `--flood` and negative counts never terminate
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/traffic/hping3_utils.py:80,108-109`

`--flood` ignores `-c` (runtime: 444,728 packets in 3 s and still running), and
`-c -1` sends indefinitely. The scheduler abandons the flow on timeout but the
container keeps flooding.

**Fix:** Reject `count < 1`; forbid `flood` in finite flow APIs or wrap it in a hard
timeout; validate `proto`, `rate_type`, and flags.

## TRAFFIC-06 — UDP `iperf3` with `bandwidth_mbps=0` runs at 1 Mbps, not unlimited
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/traffic/iperf3_utils.py:118-121`, `docs/traffic.md:89`

The `-b` flag is emitted only when `bandwidth_mbps > 0`. iperf3's UDP default is
1 Mbit/s; with `-b 0` it reached ~37.7 Gbps on loopback. The documented default
(“0 means unlimited”) is therefore wrong for UDP.

**Fix:** For UDP always emit `-b {bandwidth_mbps}M` including `-b 0`; reject
negative values.

## TRAFFIC-07 — UDP receiver metrics read missing/zero fields
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/traffic/iperf3_utils.py:401-403,426-428`

`total_bytes` reads `end.sum.bytes` (0 on the server-side UDP receiver JSON; the
byte count is under `end.sum_received.bytes`), and `total_out_of_order` reads
`end.sum.out_of_order` (the field lives under `end.streams[*].udp`). Normal UDP
flows report zero bytes and zero reordering.

**Fix:** Select `sum_received`/`sum_sent` by direction, fall back to interval sums,
and aggregate `streams[*].udp.out_of_order`.

## TRAFFIC-08 — `iperf3` JSON side selection ignores `reverse`/`bidir`
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/traffic/iperf3_utils.py:837-844`

For UDP the destination JSON is always preferred, assuming the destination is the
receiver. With `--reverse`, the destination is the sender and receiver-side
jitter/loss are on the client; TCP reverse lacks `rtt`/`snd_cwnd` on the client.
The documented options therefore report zeros or wrong metrics.

**Fix:** Choose/merge JSON by `start.test_start.reverse`, `bidir`, and
`end.sum.sender`: receiver for loss/jitter, sender for RTT/CWND/retransmits.

## TRAFFIC-09 — Throughput aggregates include omitted warm-up intervals
**Severity:** Medium · **Confidence:** High (Runtime) · **Location:** `satgonetem/traffic/iperf3_utils.py:220-231,293-341`

Every interval row, including `omitted: true`, is aggregated; the mean is over
interval rows rather than total bytes/time. `omit` therefore fails to exclude
warm-up, and the trailing short interval can spike UDP results.

**Fix:** Filter `omitted`, and compute `sum(bytes)*8 / sum(seconds)` (or use
`end.sum`/`sum_received`); apply the same filter in plotting.

## TRAFFIC-10 — iperf3 setup can die before the `try`, leaving status RUNNING forever
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/traffic/iperf3_utils.py:809-867`

`dst_ip = self.destination.loopback.ipv4` (and the temp-path construction) run
before the `try`. A missing/unset loopback raises before any status transition.
Cleanup `rm` calls sit inside the `except` and can themselves raise, preventing
`_error`/`_status` assignment. The success path also performs cleanup before storing
the result, so a cleanup failure discards a valid measurement and can leave
`RUNNING`. No path kills the detached `iperf3 -s -1` server.

**Fix:** Wrap the entire body in a top-level `try/finally`; set result/status before
best-effort cleanup; suppress cleanup errors; kill the flow's server on failure.

## TRAFFIC-11 — `cat` failure bypasses the client/server JSON fallback
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/traffic/iperf3_utils.py:838-844`

The fallback only handles a returned non-JSON string. If the preferred file is
missing, `cat` exits non-zero and `execute_command()` raises before the alternative
is tried.

**Fix:** Use `cat <path> 2>/dev/null || true` (or catch `RuntimeError`), then
validate content before switching sides.

## TRAFFIC-12 — Concurrent iperf3 flows to one destination collide on port 5201
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/traffic/iperf3_utils.py:67,826-830`, `services/mixins/traffic_testing.py:120-129`

`run_iperf3()` reuses one config for every source/destination pair, so two flows to
the same destination start two `iperf3 -s -1` servers on the same address/port. One
bind fails or a client attaches to the wrong server; `-1` makes the server exit
after the first client.

**Fix:** Allocate a unique port per flow (or per destination), pass it to both
server and client, and verify the server is listening before starting the client.

## TRAFFIC-13 — `Iperf3Config` has no validation; negative duration runs forever
**Severity:** Medium · **Confidence:** High (Runtime) · **Location:** `satgonetem/traffic/iperf3_utils.py:62-67,111-116`

`duration=-1` makes iperf3 produce intervals until killed; `parallel=0`, `port=0`,
negative window/TTL/TOS, and `num_bytes<=0` are accepted and fail inside the tool.

**Fix:** Add `__post_init__` validation (`duration>=1` unless `num_bytes>0`,
`parallel>=1`, `1<=port<=65535`, `interval>0`, non-negative sizes).

## TRAFFIC-14 — Summary-only DataFrames hide stream-only TCP metrics
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/traffic/iperf3_utils.py:522-528,1156-1184`

TCP summary rows lack `rtt`, `rttvar`, `snd_cwnd`, `snd_wnd`, and `pmtu`; those
exist only in `streams`. Plotting/statistics prefer summaries and silently skip
every TCP RTT/CWND/PMTU plot or print “Not available”.

**Fix:** Fall back to `_df_streams` for missing metrics; compute printed RTT from
stream rows.

## TRAFFIC-15 — 32-bit `run_id` can collide on large schedules
**Severity:** Low · **Confidence:** High (probability) · **Location:** `satgonetem/traffic/iperf3_utils.py:814-820`

`uuid4().hex[:8]` gives ~50 % collision probability around 77k same-pair runs; a
million-run trace is near-certain. Colliding flows share temp JSON files.

**Fix:** Use the full UUID (or ≥16 hex chars) or create per-flow temp directories.

## TRAFFIC-16 — Scheduler timeout abandons running flows; default truncates valid tests
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/traffic/flow_scheduler.py:104-107,144`

`_execute_flow()` raises `TimeoutError` after `join(timeout)` but never calls
`flow.stop()` or kills the tool. The worker is freed, so more than `max_workers`
processes can run. The 180 s default is shorter than a valid 300 s iperf3 test.

**Fix:** On timeout call a scoped `flow.stop()`/kill and join; default
`flow_timeout_sec` to `None` or derive it from the flow's configured duration.

## TRAFFIC-17 — `stop()` during a flow delay does not cancel the flow
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/traffic/ping_utils.py:335`, `hping3_utils.py:439`, `iperf3_utils.py:811`

`_run()` uses `time.sleep(delay)` without checking `_stop_event`; a stop during the
delay returns from `join()` while the thread later starts the tool anyway.

**Fix:** Use `if self._stop_event.wait(delay): return` and re-check the event before
starting the command; set a cancelled/stopped terminal status.

## TRAFFIC-18 — `stop()` kills every same-named process in the container
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/traffic/ping_utils.py:295`, `hping3_utils.py:399`, `iperf3_utils.py:770-771`

`pkill -x ping|hping3|iperf3` is container-wide; stopping one flow kills all other
flows' processes, including iperf3 servers belonging to other flows.

**Fix:** Launch each flow with a unique tag/PID file and kill by tag/PID; for
iperf3 use the per-flow port and match server/client separately.

## TRAFFIC-19 — `generate_iperf2_plots()` raises `UnboundLocalError` for TCP logs
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/traffic/iperf2_utils.py:200-213`

`total_mb` is assigned only in the UDP `else` branch. For TCP logs with data rows,
the final print raises `UnboundLocalError: local variable 'total_mb' referenced
before assignment`.

**Fix:** Compute `total_mb` once in a common branch (excluding summary rows).

## TRAFFIC-20 — iperf2 parser only accepts MBytes/Mbits
**Severity:** Medium · **Confidence:** High (Runtime) · **Location:** `satgonetem/traffic/iperf2_utils.py:34-39`

Real high-rate logs use `GBytes`/`Gbits`; the regex misses them entirely, and a
UDP sender line (no jitter/loss) is misclassified as TCP. Typical logs produce
“No valid iperf2 data found”.

**Fix:** Parse `[KMG]Bytes`/`[KMG]bits/sec` with multipliers; make jitter/loss
optional and detect UDP from the server report or the `-u` header.

## TRAFFIC-21 — iperf2 summary detection by “duration > 1 s” is wrong
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/traffic/iperf2_utils.py:54,77-88`

Any UDP interval longer than 1 s (e.g. `-i 2`) is classified as a summary, leaving
no data rows. TCP has no summary detection at all, so the final full-test row is
double-counted (cumulative bytes roughly doubled).

**Fix:** Detect summary rows structurally (final row with `start==0` and maximum
`end`, or `[SUM]`/`[Total]`) for both protocols; aggregate multi-socket summaries.

## TRAFFIC-22 — Unquoted config/node values are interpolated into `sh -c`
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/traffic/iperf3_utils.py:129-148,838-846`, `hping3_utils.py:102-113`, `models/node.py:82`

`Node._prepare_command()` wraps strings in `sh -c`. `window_size`, `length`,
`congestion_control`, `affinity`, `spoof_src`, node names in temp paths, and iperf2
values are interpolated without `shlex.quote`; whitespace splits flags and `;`,
`$(...)`, or backticks execute commands in the container.

**Fix:** `shlex.quote` every interpolated value or build argument lists and avoid
the shell; sanitize node names used in paths.

## TRAFFIC-23 — hping3 sequence/RTT arrays ignore actual sequence numbers
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/traffic/hping3_utils.py:155-164`

The parser never reads `seq=`, renumbers replies `1..N`, and inserts no placeholders
for lost packets, so sequence numbers no longer match the wire and
`cumulative_mbit` counts only replies.

**Fix:** Parse `seq=(\d+)`, preserve real sequences, insert `NaN` for gaps.

## TRAFFIC-24 — `window_size` is ignored for UDP
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/traffic/iperf3_utils.py:118-134`

The `-w` flag is only emitted in the TCP branch although the config documents the
socket buffer without a protocol restriction.

**Fix:** Move the `window_size` branch outside the protocol conditional, or
document it as TCP-only.

## TRAFFIC-25 — `save_results=False` retains all results until the last submission
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/traffic/flow_scheduler.py:278-294`

All flows are submitted (sleeping until each delay) before the completion loop
starts. Results/Futures for early bursts are retained until the last flow's
deadline, defeating the memory guarantee for million-flow schedules.

**Fix:** Interleave submission with completion processing (short-timeout waits or a
dedicated reaper thread) so completed results are discarded immediately.

## TRAFFIC-26 — Throughput aggregates mix `sum`, `sum_sent`, and `sum_received`
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/traffic/iperf3_utils.py:220-231,328-341`

Each interval contributes up to three summary rows with different rates. Averaging
all rows biases `avg/min/max_throughput_mbps` (e.g. 100/100/98 Mbps rows give 99.33
instead of 100) and worsens for reverse/bidir.

**Fix:** Select one summary series per interval (normally `sum`; explicit
`sum_received` for UDP direction) before aggregating.

---

# 6. Utilities and project builder

## UTIL-01 — Custom connection strategies are never registered
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/utils/custom_connection_strategies.py:27,345`, `satgonetem/utils/__init__.py:1`

Registration happens via decorators at import time, but no module imports
`custom_connection_strategies` (`grep` finds zero imports; `utils/__init__.py` is
only a comment). Selecting the documented strategy names leaves the registry entry
`None`, producing
`AttributeError: 'NoneType' object has no attribute 'compute_ground_object_connection_strategy'`.

**Fix:** Import `satgonetem.utils.custom_connection_strategies` from
`utils/__init__.py` or immediately after `apply_satcom_fix()` in
`topology_satcom.py`, before any `SimulationManager` is constructed.

## UTIL-02 — Both custom strategies duplicate retained connections
**Severity:** High · **Confidence:** High (Runtime per lane) · **Location:** `satgonetem/utils/custom_connection_strategies.py:322-340,455-476`

The “keep existing” pass appends connections, then the second pass iterates **all**
visible satellites, including those already appended, and appends them again.
With `maximum_connected_satellites=3`, a retained satellite appears twice, producing
duplicate `GroundStationLink` objects.

**Fix:** Track the set of satellites already appended and skip them in the second
pass (or build the second pass only from not-yet-connected satellites).

## UTIL-03 — `LongestConnectionTimeStrategy` sorts `None` estimates and crashes
**Severity:** High · **Confidence:** High (Runtime per lane) · **Location:** `satgonetem/utils/custom_connection_strategies.py:320`

`estimate_time_until_disconnection_2()` returns `None` when no positive root exists
(common for rising satellites under the straight-line model). The list is then
sorted by the estimate, raising
`TypeError: '>' not supported between instances of 'float' and 'NoneType'`.

**Fix:** Return `max_time` instead of `None` for “no future crossing”, and coerce
`None` to `max_time` before sorting; preferably use numerical propagation.

## UTIL-04 — Custom strategies crash when `maximum_connected_satellites` is `None`
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/utils/custom_connection_strategies.py:291-293,236-237,419-421`

The schema default for `maximum_connected_satellites` is `None`, but the code
compares `len(...) >= maximum_number_of_links`, raising `TypeError`.

**Fix:** Normalize `None` to unlimited (`math.inf`) before all comparisons.

## UTIL-05 — `LongestConnectionTimeStrategy` runs the estimator twice and prints every tick
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/utils/custom_connection_strategies.py:303-316`

The second estimate is only printed; both `print()` calls flood stdout in the
real-time loop and double the cost per visible satellite.

**Fix:** Remove the duplicate call and debug prints.

## UTIL-06 — SGP4 monkey-patch misses the already-imported `generate_satellite` alias
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/utils/satcom_fix.py:257-267`

Only `sat_com_trajectopy.orbital_models.PyOrbitalModel` is replaced.
`generate_satellite.py` did `from ... import PyOrbitalModel` at import time; if
`project_builder` (which imports it) loads before `topology_satcom`, the alias stays
bound to the original pyorbital class. The documented quick-start imports
`project_builder` first, so Walker satellites use a different propagation model
than custom satellites.

**Fix:** Also patch `sat_com_trajectopy.satellites.generate_satellite.PyOrbitalModel`,
or apply the patch before importing `sat_com_builder.configuration_manager`.

## UTIL-07 — `_get_orbital_elements()` crashes on pyorbital-based satellites
**Severity:** High · **Confidence:** High · **Location:** `satgonetem/utils/project_builder.py:317-343`

The function assumes an SGP4 `Satrec` (`orb.nodeo`, `orb.inclo`). A pyorbital
`Orbital` (the unpatched path from UTIL-06) has neither, so `add_mesh_links()`
raises `AttributeError: 'Orbital' object has no attribute 'nodeo'`.

**Fix:** Guard with `hasattr`, derive RAAN/phase from the TLE or propagated
position/velocity, and make UTIL-06 deterministic.

## UTIL-08 — `create_test_project()` derives `end_date` from wall-clock time
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/utils/project_builder.py:631-635` (same at `724-728`)

The docstring says `end_date` defaults to 10 minutes after `start_date`, but the
code always uses `now + 10 min`. Runtime: `start_date="01/01/2024 00:00:00"`
produced `end_date="26/10/2026 16:07:19"` — a ~2.7-year simulation. A future start
produces `end_date < start_date`.

**Fix:** Parse the effective `start_date` and compute the default end from it.

## UTIL-09 — Documented custom-ground-station flow crashes
**Severity:** High · **Confidence:** High (Runtime) · **Location:** `satgonetem/utils/project_builder.py:266-312`, `docs/custom_project.md:356-358`

`create_custom_ground_station()` sets only the ownership `domain` string; it never
sets `ground_object.ground_object_domain`. The documented flow calls
`sim_manager.update_ground_station_links()` immediately, which raises
`AttributeError: 'NoneType' object has no attribute 'ground_to_space_connections_strategy'`
(reproduced). A later `create_custom_satellite()` fails at the same point.

**Fix:** Accept/attach a `GroundObjectDomain` in the builder (or clone a default
domain), or make `update_ground_station_links()` skip stations without a domain.

## UTIL-10 — Ground-station files use a fixed, unsanitized `/tmp/<name>.txt`
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/utils/project_builder.py:94-95,636-646`

Every project with the default identifier writes the same
`/tmp/ground_stations.txt`; creating a second project overwrites the first's data.
Only spaces are sanitized, so `/`/`..` can escape the base directory; files are
never cleaned up.

**Fix:** Use a unique per-project temp directory/file, sanitize the identifier, and
resolve the path under the intended base directory.

## UTIL-11 — CSV elevation is written in km but consumed as metres
**Severity:** Medium · **Confidence:** High (Runtime) · **Location:** `satgonetem/utils/project_builder.py:59`

`GroundStationEntry.to_csv_line()` writes `elevation_km` raw; the sat_com loader
treats column 5 as metres. Runtime: the default Berlin station loads at
`0.034 m` instead of `34 m`; the custom API correctly writes `elevation_km * 1000`.

**Fix:** Write `elevation_km * 1000` (and adjust `from_csv`), or document/coerce the
loader to metres.

## UTIL-12 — `create_and_load_simulation()` ignores `project_name`
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/utils/project_builder.py:190-206`

The argument is never referenced; the name comes from `SimulationProperty`.
Callers cannot override it.

**Fix:** Copy the config dict, set `simulation_name = project_name`, then load.

## UTIL-13 — `add_mesh_links()` creates duplicate links
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/utils/project_builder.py:540-548`

For a two-satellite plane, the loop creates `0→1` and `1→0`; existing pairs are
not checked, so calling it on a topology with Walker links duplicates them.

**Fix:** Skip the reverse edge for `n == 2` and skip already-connected pairs.

## UTIL-14 — `coverage_percentage()` returns 0 % for a satellite exactly over a pole
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/utils/coverage.py:63-65,125`

At `lat0=±90` the ring collapses to a constant-latitude line; the longitude span is
π, below the `1.5π` wrap threshold, so the polygon has zero area. The slow method
returns 0.0 % while the fast method returns ~1.704 % for the same case.

**Fix:** Special-case near-polar subpoints or degenerate rings; delegate to the
spherical-cap test.

## UTIL-15 — `IPUtils` advertises a 14-bit range for a 13-bit field
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/utils/ip_utils.py:51,68-71,85-93`

The node/peer fields are 13 bits (0–8191) but the validation message says
“0 and 16383”; ids 8192–16383 pass the check and then generate malformed 33-bit
IP strings. `int()` also accepts `1_0.2.3.4`, `+1.2.3.4`, and whitespace-padded
octets as valid IPv4.

**Fix:** Correct the bound/message to 8191 (or redesign the layout), and validate
with `ipaddress.IPv4Address` or a strict regex.

## UTIL-16 — `ecef_to_eci()` applies the inverse rotation
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/utils/test.py:71`

The matrix is the ECI→ECEF rotation; ECEF longitude 0 at GMST 280.46° returns ECI
79.54° instead of 280.46°. `geodetic_to_orbital_sgp4()` inherits the error. The
helper is unused by production code today.

**Fix:** Use `[[cos, -sin], [sin, cos]]` (or `R.T.dot(r_ecef)`).

## UTIL-17 — Aware non-UTC times mix local and UTC components in `satcom_fix`
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/utils/satcom_fix.py:94-101` (compare `189-192`)

`_compute_position_at()` passes the raw local components of an aware `t` to
`jday()` but passes `Time(t, scale="utc")` to the TEME→ITRS transform. For
`2024-05-30 02:00+02:00`, the propagator treats 02:00 as UTC while Astropy converts
the same datetime to 00:00 UTC for the rotation, so the inertial position and the
Earth rotation are two hours apart and the resulting longitude is wrong.

**Fix:** Normalize aware times with `t = t.astimezone(timezone.utc)` at function
entry, then use that same object for both `jday()` and `Time(...)`.

## UTIL-18 — Pytest cannot collect `tests/utils/test_project_builder.py`
**Severity:** Medium · **Confidence:** High (Runtime) · **Location:** `tests/utils/test_project_builder.py:12,21-23`, `satgonetem/utils/project_builder.py:314`

The test imports three `_find_*` helpers that were never implemented (they do not
exist in any commit in `project_builder.py`). The whole test module contributes
zero tests, and `pytest tests` exits with a collection error.

**Fix:** Implement the helpers with the expected error messages (the tests define
their contract), or rewrite the tests to use the existing explicit lookup patterns.

## UTIL-19 — `estimate_time_until_disconnection()` is broken with both movement models
**Severity:** Medium · **Confidence:** High · **Location:** `satgonetem/utils/custom_connection_strategies.py:185-195`

With the patched model, `orbital_object` is an `sgp4.api.Satrec` and has no
`get_lonlatalt()`; the fallback calls `movement_model.get_position_at()`, which
`PyOrbitalModel` does not implement, so the method always raises `AttributeError`.
With the original pyorbital model, `get_lonlatalt()` returns altitude in kilometres
while `get_elevation_angle()` expects metres, producing wrong elevations.

**Fix:** Use `movement_model.get_longitude_latitude_altitude(delta_t)` (metres) or
convert `alt * 1000`, and remove the nonexistent `get_position_at()` fallback.

---

# 7. Packaging, metadata, and documentation

## MISC-01 — `setup.py` overrides the Python requirement at wheel build time
**Severity:** Low · **Confidence:** High (Runtime) · **Location:** `setup.py:16`, `pyproject.toml:10`

The built wheel metadata reports `Requires-Python: >=3.8` (setup.py wins), although
`pyproject.toml` and the README say 3.10+ and the code uses 3.10+ typing syntax. A
valid 3.8/3.9 install can succeed and then fail at runtime. `setup.py` also carries
stale classifiers/dependencies (missing `pyroute2`, `igraph`, `itur`, `yaspin`).

**Fix:** Remove the duplicate metadata from `setup.py` (keep only the build shim) or
set `python_requires=">=3.10"` and mirror the dependencies; add a wheel-metadata test.

## MISC-02 — `network_launcher` is documented but never used
**Severity:** Low · **Confidence:** High · **Location:** `satgonetem/services/topology_satcom.py:97,676`, `README.md:293`

The README documents a `"DIRECT"` default and a `"GONETEM"` alternative, but the
config field is only stored; `start_gonetem()` always instantiates
`GoNetEmLauncher`. There is no `"DIRECT"` launcher implementation in the package.

**Fix:** Either implement/select the launcher from the field or remove the field and
correct the README to state that GoNetEm is the only backend.

## MISC-03 — README architecture lists a nonexistent `dynamics/` package
**Severity:** Low · **Confidence:** High · **Location:** `README.md:225`

`satgonetem/` contains no `dynamics` directory or `DynamicsModel`; the architecture
diagram is stale.

**Fix:** Remove the line or restore the package if it is intended.

## MISC-04 — README calls IS-IS SR an FRR feature, code uses Bird
**Severity:** Low · **Confidence:** High · **Location:** `README.md:250`, `satgonetem/routing/isis_sr_bird_daemon.py`

The table says “IS-IS SR (FRR)” but the implementation generates Bird configuration,
and the shipped Bird image does not support it (ROUTE-10). Users are misled about
which daemon/image is required.

**Fix:** Correct the documentation and either target FRR or ship the required Bird
fork.

---

# 8. Consolidated fix plan

## Phase 0 — Stop the bleeding (do first; no API redesign)

| Order | IDs | Why |
|---|---|---|
| 1 | **LINK-01** | Fixes wrong results for every link-budget GSL. |
| 2 | **LAUNCH-01, LAUNCH-02** | A failed start must not kill the host process or leave an unclosable project. |
| 3 | **LAUNCH-03** | Prevent one project's force-close from stopping unrelated projects. |
| 4 | **ROUTE-01** | Stale static routes cause blackholes after any topology change. |
| 5 | **CORE-10** | One UT link currently kills the simulation loop every tick. |
| 6 | **LAUNCH-05, CORE-13** | HIL is nonfunctional without filtering and delete-before-add. |
| 7 | **CORE-06, CORE-07** | Failed startup/shutdown must not leak or permanently lock the launcher. |

Recommended regression tests: overhead-elevation budget, static route
add/remove, duplicate `ProjectOpen`, force-close name scoping, HIL handover order.

## Phase 1 — Restore documented features (next 1–2 weeks)

- **Link budget:** LINK-02, LINK-03, LINK-04, LINK-05, LINK-06, LINK-07, LINK-08,
  LINK-10, LINK-11, LINK-12. Add a numeric end-to-end budget test that uses real
  `Antenna` objects and checks elevation, EIRP, G/T, and capacity.
- **Routing:** ROUTE-02 through ROUTE-12. Add a fake-executor test for route
  deletion and per-node failure accounting; validate generated Bird configs with
  `bird -p` in CI using the shipped image.
- **Traffic:** TRAFFIC-01, TRAFFIC-02, TRAFFIC-05, TRAFFIC-06, TRAFFIC-07,
  TRAFFIC-08, TRAFFIC-10, TRAFFIC-12, TRAFFIC-16, TRAFFIC-19. Add real-tool parser
  fixtures for loss/reverse/UDP and a two-flow same-destination test.
- **Lifecycle:** CORE-01, CORE-02, CORE-03, CORE-08, CORE-09, CORE-12,
  CORE-17, CORE-18, CORE-19.
- **Utilities:** UTIL-01, UTIL-02, UTIL-03, UTIL-06, UTIL-07, UTIL-08, UTIL-09.

## Phase 2 — Robustness and resource handling (weeks 3–4)

- HIL: LAUNCH-13 through LAUNCH-17 (transactional setup, teardown retry,
  project-scoped names, subprocess timeouts).
- Launcher: LAUNCH-07, LAUNCH-08, LAUNCH-09, LAUNCH-18.
- Traffic: TRAFFIC-03, TRAFFIC-04, TRAFFIC-09, TRAFFIC-13, TRAFFIC-17,
  TRAFFIC-18, TRAFFIC-20 through TRAFFIC-26.
- Services/models/utils: CORE-04, CORE-05, CORE-11, CORE-14 through CORE-22,
  LINK-13 through LINK-17, UTIL-04, UTIL-05, UTIL-10 through UTIL-17.
- Tests: UTIL-18 must be fixed before Phase 1 can be validated by the full suite.

## Phase 3 — Packaging, docs, cleanup

- MISC-01 through MISC-04.
- LAUNCH-10, LAUNCH-11, LAUNCH-12, ROUTE-15 through ROUTE-17, TRAFFIC-11,
  TRAFFIC-14, TRAFFIC-15, TRAFFIC-23, TRAFFIC-24.
- Remove dead code (`utils/test.py` helpers if unused) and stale imports flagged by
  `ruff`.
- Add CI: ruff (F/E9/B), import sweep, wheel metadata check, and a `bird -p`
  configuration smoke test.

## Verification strategy after fixes

1. `pytest tests -q` must complete with zero collection errors (UTIL-18 first).
2. Add targeted tests for every Critical/High ID; run them with `-k` during
   development and the full suite once at the end.
3. Add an integration test (Docker/GoNetEm) for: start → budget link → traffic →
   link failure → stop. This is the only way to catch the launcher/HIL/routing
   contract bugs.
4. For every fix, verify both the Python model and the kernel/emulator state:
   route tables (`ip route`, `ip -f mpls route`), qdiscs (`tc qdisc show`), and
   container presence (`docker ps`).
5. Keep the no-Docker unit suite fast; gate network tests behind an env flag.

---

# Appendix A — Test baseline and reproduction environment

```
Python: 3.10.12
venv: /tmp/opencode/sgnt-venv (editable install)
pytest: 475 passed, 1 skipped, 4 warnings, 1 collection error in 42.65 s
ruff: 36 diagnostics (9 F401/F841/F821 class issues; 2 F821 are type-only)
mypy: mixin/protobuf false positives, no new concrete call errors beyond those reported
```

Reproduction highlights:

- `Link._compute_elevation_angle`: overhead geometry returns 90°, link returns 0°.
- `LinkBudgetCalculator.compute_one_way_shannon_capacity` with an aperture antenna:
  `TypeError: unsupported operand type(s) for +: 'float' and 'NoneType'`.
- `create_test_project(start_date="01/01/2024 00:00:00")`: `end_date` is
  `26/10/2026 16:07:19` (wall-clock + 10 min).
- Default CSV ground stations load with `altitude=0.034 m`; custom API gives 34 m.
- `add_custom_ground_station` + `update_ground_station_links`: `AttributeError:
  'NoneType' object has no attribute 'ground_to_space_connections_strategy'`.
- Shipped image `jariassuarez/sgnt:satellite`: Bird 2.0.7 rejects `hostname` and
  `protocol mpls`; `/usr/bin/start-service.py` is absent.
- `ip -f mpls route flush` prints `"ip route flush" requires arguments` and returns
  success only because of `; true`.
- `from satgonetem.proto import netem_pb2` in a clean process:
  `TypeError: Couldn't build proto file into descriptor pool`.

# Appendix B — Claims investigated and rejected

These were reported by reviewers or looked suspicious, but were disproven or are
not defects:

1. **“HIL netem/TBF order is invalid”** — Tested in a user network namespace:
   `tc qdisc add dev dummy0 root handle 1: netem ...` followed by
   `tc qdisc add dev dummy0 parent 1:1 handle 10: tbf ...` is accepted, and
   `tc qdisc show` reports the TBF child under netem. **Not a bug.**
2. **“`pyproject.toml` `packages = ["satgonetem"]` omits subpackages”** — A built
   wheel contains all 66 Python modules, including subpackages, because `setup.py`
  's `find_packages()` contributes them. **Not a packaging bug**; the real
   packaging issue is MISC-01 (Python requirement conflict).
3. **“`distance_3d_km()` raises `ValueError` near the antipode”** — Not reproduced
   in 20,000 random pairs; the formula is still wrong (LINK-12), and clamping `a`
   remains a defensive improvement.
4. **“Direct import of `netem_pb2` fails”** — Real, but only in a clean process
   before `netem_pb2_grpc`/`empty_pb2` is imported; normal startup masks it
   (LAUNCH-11).
5. **“`compute_downlink_modcod` EIRP behavior”** — The function intentionally
   requires a pre-computed EIRP in its current tests; whether that is desired is a
   documentation/API decision (LINK-06).

# Appendix C — Suspected issues needing runtime/environment proof (not counted)

1. **HIL host namespace assumption** — `HOST_NS_PATH = "/proc/1/ns/net"`
   (`hil_manager.py:46`) assumes satgonetem runs in the host PID/net namespace. In a
   containerized deployment, PID 1 is the container init and the hardware interface
   is not visible. Fix: make the host netns path configurable.
2. **`force_close_project()` does not consume the `ProjectClose` stream** —
   `self._client.ProjectClose(self._request)` is called and the channel is closed
   immediately (`gonetem_launcher.py:454-461`). The server may still consider the
   project open. Verify against a real GoNetEm server.
3. **OSPF/IS-IS never rewrite configs on pure link deletion** — `update()` rebuilds
   only nodes in `new_links`; a deletion may leave stale interface clauses. Verify
   with a live Bird node before changing.
4. **iperf3 fixed startup/read sleeps** — 0.5 s before the client and 0.3 s before
   `cat` (`iperf3_utils.py:831-835`); slower hosts can hit connection-refused or
   partial JSON. Add readiness checks.
5. **4 MiB gRPC receive limit** — `NodeRun` returns all stdout in one message;
   long iperf3 JSON / high-count hping3 output may exceed the default. Verify and
   add streaming/chunking if needed.
6. **SR-MPLS PHP penultimate-hop routes** — With `use_php=True`, non-addressable
   penultimate satellites may lack a `/32` route to the destination loopback. The
   feature is currently disabled by default; test before enabling.
7. **`get_current_graph()` disabled edges in static mode** — Confirmed structurally
   and by lane runtime (451 edges, 324 disabled), but the default dynamic topology
   has no disabled edges at t=0. Reproduce with
   `static_ground_station_link_mode=True` before prioritizing.
8. **iperf3 stderr merged into JSON** — Both client and server redirect `2>&1` into
   the JSON file (`iperf3_utils.py:151,827-828,909-911`). A warning emitted by the
   selected side (e.g. an unsupported congestion-control algorithm) appears before
   `{` and breaks parsing. Needs a run on a host where the selected side warns.
9. **Duplicate links between the same endpoint pair collapse to one** — The key is
   an unordered `frozenset` (`topology_sync.py:351-443`), so two sat_com links
   between the same pair overwrite each other in `self.links`. Reproduce with a
   topology that deliberately creates duplicate pairs (see UTIL-13).
10. **`NodeTerminal` request generator may block after stdin EOF** — The generator
    waits on `input_queue.get()`; the sentinel is only queued in `recv_loop`'s
    `finally`, not when the local main loop exits on EOF (`node_terminal_bridge.py:80-166`).
    Current gRPC uses a daemon thread, so no hang was reproduced; verify on the
    supported grpcio versions.

---

*End of report. No fixes were applied.*
