# Link Budget

The link budget module computes satellite-ground link capacities from physical RF parameters instead of using static defaults. When enabled, every `GroundStationLink` gets its `peer1_capacity` (downlink) and `peer2_capacity` (uplink) recalculated from antenna gains, free-space loss, atmospheric attenuation, and a chosen capacity strategy.

## Architecture

```
TopologyManager
    |
    +-- link_budget_config: LinkBudgetConfig
    |
    +-- set_antenna(nodes, AntennaConfig)
            |
            +-- node.antenna = Antenna(...)
    |
    +-- Link (GroundStationLink)
            |
            +-- update_link_capacities()
                    |
                    +-- _compute_budget_capacity()
                            |
                            +-- LinkBudgetService
                                    |
                                    +-- Transmitter EIRP
                                    +-- Receiver G/T
                                    +-- Free-space loss
                                    +-- Atmospheric attenuation (itur)
                                    +-- CapacityStrategy
                                            |
                                            +-- ModCodCapacityStrategy  (downlink)
                                            +-- ShannonCapacityStrategy (uplink)
```

The calculation follows the classic chain: **EIRP → FSL + Atmospheric → C/N₀ → Strategy → Capacity**.

Atmospheric attenuation requires the optional `itur` package. Install it with:

```bash
pip install satgonetem[extra]
```

Without `itur`, budget computation falls back to the default static capacity.

---

## Enabling link budget

Set `use_budget=True` on the `TopologyManager` or in `NetworkConfig` before construction:

```python
from satgonetem.services.topology_satcom import TopologyManager, NetworkConfig

net_cfg = NetworkConfig(
    use_budget=True,
    gnd_link_capacity=100000,  # fallback when budget is unavailable
)

topology = TopologyManager.from_satcom(project, net_cfg)
```

Only `GroundStationLink` objects are affected. ISLs always keep their static `isl_link_capacity`.

---

## Configuration classes

### LinkBudgetConfig

`LinkBudgetConfig` holds the carrier frequencies and bandwidths used for downlink (satellite → ground) and uplink (ground → satellite). It is stored on the `TopologyManager` and passed automatically to every new `Link`.

```python
from satgonetem.link_budget.config import LinkBudgetConfig

cfg = LinkBudgetConfig(
    downlink_freq_ghz=19.0,      # satellite → ground
    uplink_freq_ghz=14.25,       # ground → satellite
    bandwidth_hz_downlink=500e6, # downlink bandwidth in Hz
    bandwidth_hz_uplink=500e6,   # uplink bandwidth in Hz
)
```

**Fields**

| Field | Type | Default | Description |
|---|---|---|---|
| `downlink_freq_ghz` | `float` | `19.0` | Downlink carrier frequency in GHz |
| `uplink_freq_ghz` | `float` | `14.25` | Uplink carrier frequency in GHz |
| `bandwidth_hz_downlink` | `float` | `500e6` | Downlink signal bandwidth in Hz |
| `bandwidth_hz_uplink` | `float` | `500e6` | Uplink signal bandwidth in Hz |

Apply it to the topology manager with `set_link_budget_config`:

```python
topology.set_link_budget_config(cfg)
```

This stores the config on the manager and propagates it to all existing links. New links created afterwards inherit it automatically.

---

### AntennaConfig

`AntennaConfig` is a plain dataclass that describes an antenna. It is converted into an `Antenna` object by `TopologyManager.set_antenna`.

```python
from satgonetem.link_budget.config import AntennaConfig

gnd_antenna_conf = AntennaConfig(
    diameter=2.0,
    efficiency=0.6,
    sspa_output_power_db=0.0,
    losses_db=0.0,
)

sat_antenna_conf = AntennaConfig(
    diameter=1.0,
    efficiency=0.6,
    sspa_output_power_db=40.0,
    losses_db=1.0,
)
```

**Fields**

| Field | Type | Default | Description |
|---|---|---|---|
| `diameter` | `float` | `0.0` | Aperture diameter in metres |
| `efficiency` | `float` | `0.6` | Aperture efficiency, 0 … 1 |
| `sspa_output_power_db` | `float` | `0.0` | SSPA output power in dBW |
| `losses_db` | `float` | `0.0` | Transmit / receive losses in dB |
| `eirp_db` | `float or None` | `None` | Optional pre-computed EIRP in dBW |
| `gain_db` | `float or None` | `None` | Optional pre-computed gain in dBi |

When `gain_db` is `None`, gain is computed from `diameter` and `efficiency` at the link frequency. When `eirp_db` is `None`, EIRP is derived from `sspa_output_power_db + gain_db - losses_db`.

---

## TopologyManager methods

### `set_link_budget_config(config: LinkBudgetConfig) -> None`

Apply a link-budget configuration to the manager and all existing links.

```python
from satgonetem.link_budget.config import LinkBudgetConfig

topology.set_link_budget_config(
    LinkBudgetConfig(downlink_freq_ghz=20.0, uplink_freq_ghz=15.0)
)
```

Existing `GroundStationLink` objects with `use_budget=True` have their capacities recomputed immediately. Links created after this call receive the config automatically.

---

### `set_antenna(nodes: list[Node], config: AntennaConfig) -> None`

Create an `Antenna` from `config` and attach it to every node in `nodes`.

```python
from satgonetem.link_budget.config import AntennaConfig

gnd_antenna_conf = AntennaConfig(diameter=2.0, efficiency=0.6)
sat_antenna_conf = AntennaConfig(diameter=1.0, efficiency=0.6, sspa_output_power_db=40.0)

topology.set_antenna(list(topology.ground_stations.values()), gnd_antenna_conf)
topology.set_antenna(list(topology.satellites.values()), sat_antenna_conf)
```

All nodes in the list share the same `Antenna` instance. If you need distinct instances, call `set_antenna` with single-node lists or build `Antenna` objects manually and assign them to `node.antenna` directly.

---

## Complete example

```python
from satgonetem.services.topology_satcom import TopologyManager, NetworkConfig
from satgonetem.link_budget.config import LinkBudgetConfig, AntennaConfig
from satgonetem.utils.project_builder import create_test_project

# Build a test constellation
project = create_test_project()

# Enable link budget
net_cfg = NetworkConfig(use_budget=True)
topology = TopologyManager.from_satcom(project, net_cfg)

# Set RF frequencies
topology.set_link_budget_config(
    LinkBudgetConfig(downlink_freq_ghz=19.0, uplink_freq_ghz=14.25)
)

# Assign antennas
topology.set_antenna(
    list(topology.ground_stations.values()),
    AntennaConfig(diameter=2.0, efficiency=0.6),
)
topology.set_antenna(
    list(topology.satellites.values()),
    AntennaConfig(diameter=1.0, efficiency=0.6, sspa_output_power_db=40.0),
)

# Start the emulator - GSL capacities are now computed from the budget
topology.start_gonetem()
```

---

## Low-level API

For custom scripts or plugins, the same primitives used internally are available directly.

### LinkBudgetService

`LinkBudgetService` is the high-level orchestrator. It takes `LinkBudgetInputs` and a `CapacityStrategy`, then returns a capacity in kbps.

```python
from satgonetem.link_budget import (
    LinkBudgetInputs,
    LinkBudgetService,
    ModCodCapacityStrategy,
    ShannonCapacityStrategy,
)

inputs = LinkBudgetInputs(
    tx_antenna=sat.antenna,
    rx_antenna=gs.antenna,
    frequency_ghz=19.0,
    distance_km=1000.0,
    elevation_angle=45.0,
    gs_lat=43.6,
    gs_lon=1.44,
    gs_diameter=1.2,
    bandwidth_hz=500e6,
    rx_tsys_k=100.0,
    unavailability_percent=0.1,
)

service = LinkBudgetService(capacity_strategy=ModCodCapacityStrategy())
capacity_kbps = service.compute_one_way(inputs)
```

**LinkBudgetInputs fields**

| Field | Type | Default | Description |
|---|---|---|---|
| `tx_antenna` | `Antenna` | required | Transmitting antenna |
| `rx_antenna` | `Antenna` | required | Receiving antenna |
| `frequency_ghz` | `float` | required | Carrier frequency in GHz |
| `distance_km` | `float` | required | Slant range in kilometres |
| `elevation_angle` | `float` | required | Elevation angle in degrees |
| `gs_lat` | `float` | required | Ground-station latitude |
| `gs_lon` | `float` | required | Ground-station longitude |
| `gs_diameter` | `float` | required | Ground-station antenna diameter in metres |
| `bandwidth_hz` | `float` | required | Signal bandwidth in Hz |
| `rx_tsys_k` | `float` | `100.0` | Receiver system noise temperature in K |
| `unavailability_percent` | `float` | `0.1` | Time percentage of unavailability in % |

---

### Capacity strategies

| Strategy | Description | Typical use |
|---|---|---|
| `ModCodCapacityStrategy` | DVB-S2X MODCOD selection | Downlink (realistic modem behaviour) |
| `ShannonCapacityStrategy` | Shannon theoretical limit | Uplink (upper bound) |

Both implement the `CapacityStrategy` protocol:

```python
class CapacityStrategy(Protocol):
    def compute_capacity(self, cn0_dbhz: float, bandwidth_hz: float) -> float:
        ...  # returns capacity in bps
```

`ModCodCapacityStrategy` accepts an optional `rolloff_factor` (default `0.25`).

---

### LinkBudgetCalculator

`LinkBudgetCalculator` is a convenience class that bundles frequencies, bandwidths, and thresholds into a single object with helper methods.

```python
from satgonetem.link_budget import LinkBudgetCalculator

calc = LinkBudgetCalculator(
    frequency_ghz_downlink=19.0,
    frequency_ghz_uplink=14.25,
    bandwidth_hz_downlink=500e6,
    bandwidth_hz_uplink=500e6,
    rolloff_factor=0.25,
    unavailability_percent=0.1,
    rx_tsys_k=100.0,
)

result = calc.compute_downlink_modcod(
    sat_node=sat,
    gnd_node=gs,
    distance_km=1000.0,
    gs_lat=43.6,
    gs_lon=1.44,
    gs_diameter=1.2,
)

print(result.capacity_kbps)        # int or None
print(result.elevation_angle)      # float
print(result.atmospheric_attenuation_db)  # float
```

`LinkBudgetResult` exposes every intermediate value so you can inspect the budget step by step.

---

## Fallback behaviour

If any of the following conditions are met, a link falls back to its `default_capacity_kbps`:

- `use_budget` is `False`
- The link type is not `"GroundStationLink"`
- Either endpoint has no `antenna` set
- The `itur` package is missing and atmospheric attenuation cannot be computed
- EIRP or G/T computation fails

This makes link budget safe to enable globally: links that cannot be computed simply keep their static capacity.

---

## Running tests

```bash
pytest tests/link_budget/ -v
pytest tests/models/test_link.py -v
pytest tests/services/test_topology_satcom.py -v
```
