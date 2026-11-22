# 0011 — Optional dual-stack IPv6 addressing

- Status: Implemented
- Date: 2026-11-22
- Implementation date: 2026-11-22
- Depends on: 0006, 0007
- Supersedes: none

## Context

The address plan is IPv4-only. Interfaces, loopbacks, node blocks, and link
allocation are all IPv4. Modern constellation and 5G NTN experiments need
IPv6 reachability, but adding IPv6 routing/traffic is a much larger change.
This spec adds an optional, deterministic IPv6 address plan and pushes the
addresses into containers. IPv4 behavior remains unchanged and is always
enabled.

## Requirements

1. **Planner.** Add `Ipv6AddressPlan` with the same node kinds and
   link-ownership rules as `Ipv4AddressPlan`: ground stations first, then user
   terminals, then the lower satellite ID. Node blocks use per-kind ULA pools;
   loopbacks are `/128`; point-to-point links are `/127` pairs; released link
   slots are reused lowest-first.
2. **Configuration.** Add `NetworkConfig.ipv6_enabled` (default `False`) and
   per-kind IPv6 pools/prefix lengths. Defaults: `fd00:1::/40` ground stations,
   `fd00:2::/40` satellites, `fd00:3::/40` user terminals, `/64` node blocks.
   Pools must not overlap and must be non-global ULA/private space.
3. **Models.** `Interface` stores `ipv6` and `ipv6_prefix_len` and exposes
   `set_ipv6()`/`network6`; `Node` stores an IPv6 block and can apply IPv6
   addresses to containers with one batched `ip -6 -force -batch` command.
4. **Topology integration.** When enabled, `TopologyManager` registers every
   node in both plans, allocates/releases both address families for every
   link, assigns IPv6 to newly built interfaces, and pushes IPv6 addresses
   with `set_ipv6s_for_all_nodes()`. `set_ip_addresses()` must push both
   families when IPv6 is enabled.
5. **Persistence.** `from_file(path, network_config=...)` accepts an optional
   config override so a scenario can enable IPv6 before the plan is built.
   Existing `from_file(path)` behavior is unchanged.
6. **Validation.** Invalid pools, overlapping pools, wrong address families,
   prefix lengths outside `[0, 128]`, and blocks too small for a loopback plus
   one link must raise `AddressPlanConfigurationError`.

## Acceptance criteria

- With IPv6 disabled, existing IPv4 tests and generated projects are
  byte-for-byte unchanged.
- With IPv6 enabled, every node has a unique `/64` block, a `/128` loopback at
  offset 1, and every link has a unique `/127` pair inside the owner's block.
- Link release and reallocation reuse the lowest free IPv6 slot.
- `set_ipv6s_for_all_nodes()` issues one batched command per node and raises
  after all nodes are attempted.
- `to_file`/`from_file` round-trips the new `NetworkConfig` fields.
- A topology saved with IPv6 enabled reloads with IPv6 still enabled.

## Non-goals

- IPv6 routing protocols (OSPFv3, IS-ISv6), IPv6 static routes, IPv6 traffic
  tools, DHCPv6, SLAAC, or multicast.
- Public IPv6 addressing or NAT66.
- Changing IPv4 pools, prefixes, or allocation order.

## Risks and mitigations

- **Container IPv6 support.** `ip -6 addr add` may fail in images without IPv6
  enabled. Report failures per node; do not silently skip.
- **Address-plan duplication.** Keep the IPv6 planner independent and reuse
  `NodeKind`; do not refactor the tested IPv4 planner in this change.
- **Large link counts.** `/64` blocks have ample link slots; still test slot
  reuse and exhaustion with tiny test pools.

## Verification

```bash
uv run --frozen --extra dev pytest -q tests/addressing tests/models tests/services/test_interface_mgr*
uv run --frozen --extra dev pytest -q tests/addressing/test_topology_addressing.py
uv run --frozen --extra dev ruff check satgonetem tests
uv run --frozen --extra dev mypy
```

### Verification record — 2026-11-22

- `pytest -q tests/addressing tests/models tests/services/test_interface_mgr.py
  tests/services/test_network_config.py
  tests/services/test_topology_satcom_persistence.py` — 435 passed.
- `pytest -q` — 1957 passed, 1 skipped, 9 deselected (Python 3.14).
- `ruff check satgonetem tests` — pass; `mypy` — pass (116 files).
- Unverified: `ip -6 addr replace` inside the Docker images and the
  Python 3.10–3.13 runtime matrix (supplementary tests passed under 3.10;
  CI runs the full matrix).
