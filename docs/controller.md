# Controller Plane

The **controller plane** is a dedicated out-of-band management network that runs alongside the normal data-plane topology. It is implemented as an extra OVS bridge (named `Controller` by default) inside the GoNetEm environment, plus a veth pair that links the host machine to that bridge. Every satellite and ground station receives a unique controller-plane IP address so you can reach any node directly from the host for debugging, orchestration, or external control traffic.

---

## Configuration

All controller settings are defined on the `NetworkConfig` dataclass and forwarded to `TopologyManager` at construction time.

| Attribute | Default | Description |
|-----------|---------|-------------|
| `controller_veth_host` | `veth-host` | Host-side veth interface name. |
| `controller_veth_peer` | `veth-controller` | Peer-side veth interface name (moved into the OVS container). |
| `controller_host_ip` | `248.0.0.2` | IPv4 address assigned to the host side of the veth pair. |
| `controller_subnet_prefix` | `16` | Prefix length for the controller subnet. |
| `controller_bridge_name` | `Controller` | Name of the OVS bridge inside the GoNetEm/OVS container. |
| `controller_node_iface_pattern` | `eth{node_id}` | Python format string used to pick the node interface that receives the controller IP. Must contain `{node_id}`. |

You can override any of these values when you build a `NetworkConfig`:

```python
from satgonetem.services.topology_satcom import NetworkConfig

config = NetworkConfig(
    controller_veth_host="mgmt0",
    controller_veth_peer="mgmt1",
    controller_host_ip="10.255.0.1",
    controller_subnet_prefix=24,
)

tm = TopologyManager.from_simulation_manager(sim_manager, network_config=config)
```

---

## Enabling the controller at startup

The controller plane is **opt-in**. Pass `controller=True` to `start_gonetem()`:

```python
tm.start_gonetem(controller=True)
```

After the containers are running you can create the host-side connection and assign addresses:

```python
# 1. Create the host veth pair and attach it to the Controller OVS bridge.
tm.setup_controller_connection()

# 2. Assign a controller IP to every node on the interface defined by controller_node_iface_pattern.
tm.setup_controller_addressing()
```
---

## ControllerMixin API

`ControllerMixin` is mixed into `TopologyManager`. All methods below are available on any `TopologyManager` instance.

### `setup_controller_connection()`

```python
def setup_controller_connection(self) -> None
```

Creates the veth pair that links the **host** to the **Controller OVS bridge** running inside the GoNetEm/OVS container.

**Example:**

```python
try:
    tm.setup_controller_connection()
except RuntimeError as exc:
    print(f"Controller link failed: {exc}")
```

---

### `setup_controller_addressing()`

```python
def setup_controller_addressing(self) -> dict[str, str]
```

Assigns a unique controller-plane IPv4 address to **every satellite and ground station**.

**Returns:**

* `dict[node_name, ip_address]`: the mapping of assigned addresses.

**Example:**

```python
ips = tm.setup_controller_addressing()
for name, ip in ips.items():
    print(f"{name} -> {ip}")
```

---

> **Renamed in this release:** `craft_controller_ip` → `build_controller_ip`. The old name is no longer available.

---

### `build_satellite_ip(satellite_id)` *(deprecated)*

```python
def build_satellite_ip(self, satellite_id: int) -> str
```

Deprecated alias for `build_controller_ip`. It exists only for backward compatibility and may be removed in a future release.

---

### `cleanup_controller_connection()`

```python
def cleanup_controller_connection(self) -> None
```

Tears down the host-side controller veth pair and removes the peer port from the OVS Controller bridge.

It is safe to call even if the connection was never set up. You should call this **before** `stop_gonetem()` to avoid docker complaining about busy interfaces.

**Example:**

```python
tm.cleanup_controller_connection()
tm.stop_gonetem()
```

---

### `get_management_ip(node_id)`

```python
def get_management_ip(self, node_id: int) -> str
```

Returns the management / controller IP for a given node ID..

**Example:**

```python
ip = tm.get_management_ip(sat.id)
print(f"Management IP for {sat.name} is {ip}")
```

---

### `get_management_interface(node_id)`

```python
def get_management_interface(self, node_id: int) -> str
```

Returns the management interface identifier for a node. The current implementation computes it as `50000 + node_id`. This value is useful when you need a deterministic interface index that does not collide with data-plane interface numbering.

**Example:**

```python
iface_id = tm.get_management_interface(sat.id)
print(f"Management ifindex = {iface_id}")
```

---

## Complete Example

A complete example that matches the pattern used in `main.py`:

```python
from satgonetem.services.topology_satcom import TopologyManager

# 1. Build / load the simulation manager as usual.
tm = TopologyManager.from_simulation_manager(simulation_manager)

# 2. Start GoNetEm **with** the controller plane.
tm.start_gonetem(controller=True)

# 3. Create the host veth link to the Controller OVS bridge.
tm.setup_controller_connection()

# 4. Assign controller IPs to every node.
tm.setup_controller_addressing()

# 5. Run the normal data-plane setup.
tm.set_ip_addresses()
tm.init_routing()

# --- simulation is running, host can reach every node on 248.0.y.x ---

# Get the management IP of Sat87
ip_address = tm.get_management_ip(87)

# Do something with it from the host....

# Need to bind something in the container to the interface? Get the interface first
iface_id = tm.get_management_interface(87) # returns eth50087 

# 6. Before teardown, clean up the controller link.
tm.cleanup_controller_connection()

# 7. Stop GoNetEm and the simulation.
tm.stop_gonetem()
```
