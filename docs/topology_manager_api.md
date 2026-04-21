# TopologyManager Public API

All methods listed here are part of the `TopologyManager` public interface.
Private helpers (prefixed with `_`) are excluded.

---

## Emulator lifecycle

### `start_gonetem() -> float | None`

Start the network emulator by launching containers and wiring links.

Selects the launcher backend based on `self.network_launcher`:
- `"GONETEM"` - uses `GoNetEmLauncher` (gRPC-based).
- Any other value - uses `DirectLauncher`.

If `self.hil_manager` is set, HIL-managed nodes and links are excluded from
the software launch and delegated to the HIL manager instead.

**Returns**
- `float` - elapsed wall-clock seconds on success.
- `None` - if the emulator is already running (no-op).

---

### `stop_gonetem() -> float`

Stop the network emulator and release all resources.

Tears down HIL connections, closes the launcher project, and resets routing
state and internal status flags. Call this after `stop()` to fully clean up.

**Returns**
- `float` - elapsed wall-clock seconds.

---

### `force_stop_gonetem() -> float`

Force-stop the emulator without graceful cleanup.

Issues a `ResourceWarning`. Prefer `stop_gonetem()` for normal teardown.

**Returns**
- `float` - elapsed wall-clock seconds.

---

## Simulation loop

### `start() -> None`

Start the simulation loop in a background daemon thread.

Raises `RuntimeError` if no project is loaded.

---

### `stop() -> None`

Signal the background loop to stop and join its thread.

---

### `next_step() -> None`

Advance the simulation by exactly one tick.

---

### `speed_up() -> None`

Reduce `update_factor` by 10%, clamped to a minimum of `0.01`.

---

### `speed_down() -> None`

Increase `update_factor` by 10%, clamped to a maximum of `10.0`.

---

### `set_update_time(seconds: int) -> None`

Set the simulation tick interval. Clamped to a minimum of 1 second.

**Args**
- `seconds` - desired interval in seconds.

---

## Addressing and routing

### `set_IP_addresses() -> float`

Assign IPv4 addresses to all node interfaces, including loopbacks.

**Returns**
- `float` - elapsed wall-clock seconds.

---

### `init_routing(max_workers: int = MAX_WORKERS, routing_method: str = "") -> float`

Initialize the routing daemon for the configured routing method.

Built-in methods: `"static"`, `"dynamic-ospf"`, `"dynamic-isis"`, `"sr-mpls"`.
Additional methods can be registered via `register_routing_daemon()`.

Passing `routing_method` overrides `self.routing` for this call only.

**Args**
- `max_workers` - maximum parallel threads used during route installation.
- `routing_method` - routing method name; overrides `self.routing` when non-empty.

**Returns**
- `float` - elapsed wall-clock seconds on success.
- `-1.0` - on failure or unknown method.

---

### `delete_routing() -> float`

Remove all installed routes via the active daemon and reset routing state.

**Returns**
- `float` - elapsed wall-clock seconds on success.
- `-1.0` - if routing was not active.

---

### `get_allowed_routing_methods() -> List[str]`

Return all supported routing method names, including custom-registered ones.

**Returns**
- `List[str]` - e.g. `["static", "dynamic-ospf", "dynamic-isis", "sr-mpls", ...]`.

---

## Node inspection

### `get_node_by_name(name: str) -> Optional[Node]`

Look up a satellite or ground station by its string name.

Names follow the pattern `"SatN"` for satellites and `"GndN"` for ground
stations, where `N` is the integer `topology_uniq_id`.

**Args**
- `name` - e.g. `"Sat3"` or `"Gnd1"`.

**Returns**
- The matching `Satellite` or `GroundStation`, or `None` if not found.

**Raises**
- `TypeError` - if the internal dict contains an unexpected type.

---

### `get_node_by_id(node_id: int) -> Optional[Node]`

Look up a node by its integer topology ID. Checks satellites first, then
ground stations.

**Args**
- `node_id` - the `topology_uniq_id` of the target node.

**Returns**
- The matching `Satellite` or `GroundStation`, or `None` if not found.

**Raises**
- `TypeError` - if the internal dict contains an unexpected type.

---

### `get_node_IP_addresses(name: str) -> List[str]`

Return all IPv4 addresses assigned to a node's interfaces.

**Args**
- `name` - node name string (e.g. `"Sat3"` or `"Gnd1"`).

**Returns**
- `List[str]` - non-empty IPv4 strings for each configured interface.
  Empty list if the node is not found.

---

### `get_node_ifaces(name: str) -> List[str]`

Return interface names for a node that have both a name and an IPv4 address.

**Args**
- `name` - node name string (e.g. `"Sat3"` or `"Gnd1"`).

**Returns**
- `List[str]` - interface name strings. Empty list if the node is not found
  or has no configured interfaces.

---

### `get_topology_summary() -> Dict[str, Any]`

Return a count summary of the current topology.

**Returns**

```python
{
    "satellites": int,
    "ground_stations": int,
    "links": int,
}
```

---

### `execute_command_on(node: str = "", command: str = "") -> Dict[str, Any]`

Execute a shell command inside a node's container and return the output.

**Args**
- `node` - node name string (`"SatN"` or `"GndN"`).
- `command` - shell command to run inside the container.

**Returns**

```python
{"output": str, "error": str}
```

`error` is an empty string on success, or a description of the failure.

---

## Traffic

### `ping(source, destination, config: Optional[PingConfig] = None) -> List[PingFlow]`

Send ICMP echo requests for all (source, destination) combinations.

Accepts a single `Node` or a list of `Node` objects for each argument.
Returns immediately; poll `flow.status()` for `PingStatus.DONE`.

**Args**
- `source` - `Node` or `List[Node]` sending ICMP requests.
- `destination` - `Node` or `List[Node]` as ping targets.
- `config` - `PingConfig` controlling count, timeout, and interval.
  Defaults to `PingConfig()`.

**Returns**
- `List[PingFlow]` - one flow per (source, destination) pair.

---

### `run_iperf3(source, destination, config: Iperf3Config) -> List[Iperf3Flow]`

Launch iperf3 flows for all (source, destination) combinations.

Returns immediately; poll `flow.status()` for `FlowStatus.DONE`.

**Args**
- `source` - `Node` or `List[Node]` running the iperf3 client.
- `destination` - `Node` or `List[Node]` running the iperf3 server.
- `config` - `Iperf3Config` with protocol, bandwidth, duration, etc.

**Returns**
- `List[Iperf3Flow]` - one flow per (source, destination) pair.

---

### `run_hping3(source, destination, config: Optional[Hping3Config] = None) -> List[Hping3Flow]`

Launch hping3 flows for all (source, destination) combinations.

Returns immediately; poll `flow.status()` for `Hping3Status.DONE`.

**Args**
- `source` - `Node` or `List[Node]` running hping3.
- `destination` - `Node` or `List[Node]` as targets.
- `config` - `Hping3Config` controlling protocol, port, count, and flags.
  Defaults to `Hping3Config()`.

**Returns**
- `List[Hping3Flow]` - one flow per (source, destination) pair.

---

## Packet capture

### `is_running_tcpdump() -> bool`

Check whether tcpdump is active on any satellite container.

**Returns**
- `bool` - `True` if running on at least one satellite, `False` otherwise.

---

### `enable_tcpdump_on_satellites(satellites: list = [], dump_dir: str = "/tmp/dump") -> None`

Enable tcpdump on satellites if not already running.

Requires the emulator to be active. If tcpdump is detected on any satellite,
the call is a no-op.

**Args**
- `satellites` - satellite objects to capture on. Defaults to all satellites
  when empty.
- `dump_dir` - absolute path inside each container where `.pcap` files are written.

---

### `disable_tcpdump_on_satellites(satellites: list = []) -> None`

Stop tcpdump on satellite containers by killing the process.

**Args**
- `satellites` - satellite objects to stop capturing on. Defaults to all
  satellites when empty.

---

## Coverage

### `get_coverage_percentage(elev_min_deg: float = 10.0, grid_res_deg: float = 1.0, max_latitude_deg: float = 90.0, R_earth_km: float = 6371.0) -> float`

Return the current ground coverage percentage of the constellation.

**Args**
- `elev_min_deg` - minimum elevation angle in degrees for a point to be
  considered covered.
- `grid_res_deg` - sampling grid resolution in degrees. Smaller values are
  more accurate but slower.
- `max_latitude_deg` - symmetric latitude bound of the sampling grid.
- `R_earth_km` - mean Earth radius in kilometres.

**Returns**
- `float` - coverage in the range `[0.0, 100.0]`.
