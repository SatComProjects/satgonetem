# Phase 5 design review

Scope: the optional metadata factories in `satgonetem/models/satcom_extensions.py`
and the design note in [`docs/factories.md`](factories.md).

## Findings

1. **The thin metadata layer is the right boundary.** `EmulatorSatellite`,
   `EmulatorGroundStation`, `EmulatorUserTerminal`, and
   `EmulatorPointOfPresence` add only `emulator_id`, `container_name`, and
   `address_block`. They do not duplicate `Node`, interface, routing, QoS, or
   container state. The default V5 classes remain unchanged.
2. **Factory injection is correctly opt-in.** `TopologyManager.from_satcom()` and
   `create_and_load_simulation()` accept factories without changing existing
   call sites. Metadata is filled by the initial sync and address-plan build.
3. **Custom event factories have a deliberately limited job.** They add
   container names to ISL/GSL/UTL events; the event sync uses them when present.
   They do not remove the V5 link lookup, so they are an identity/ergonomics
   improvement, not a performance fix. Event mode remains off by default.
4. **Pydantic generic warning is noisy but not structural.** The V5 `Satellite`
   generic emits `GenericBeforeBaseModelWarning` when subclassed. It does not
   break validation or runtime behavior. Fixing it cleanly is an upstream V5
   generic-ordering change; do not hide it in application code.
5. **Full `Node`/`Link` unification is rejected.** `Node` owns Docker handles,
   network namespaces, interfaces, planned `/31` addresses, routing tables,
   daemon state, and QoS. Making it a pydantic V5 model would couple two
   lifecycles and is a large, risky refactor with no functional requirement.
6. **The `/31` allocator stays in SatGoNetEm.** V5 has no link-address concept;
   the planner's node/loopback blocks and the emulator's link allocator remain
   separate concerns.
7. **Runtime-state and factory paths are independent.** Runtime state rebuilds
   V5 objects with the default factories, not the injected ones. This is
   acceptable for persistence (identity is restored from the saved state), but
   should be documented as a limitation if custom object fields must survive a
   round-trip.

## Decision

Accept the Phase 5 design as an **opt-in metadata layer**:

- keep the emulator wrappers;
- keep the `/31` allocator in SatGoNetEm;
- keep factories opt-in;
- keep event mode opt-in;
- defer full model unification.

## Action items

- Document that `from_file()` runtime restore does not re-inject custom node
  factories; custom fields that are not part of the runtime-state schema are
  not preserved.
- Revisit the pydantic generic warning only with an upstream V5 change.
- Re-benchmark event mode after satComTopology removes the no-subscriber event
  construction overhead.
