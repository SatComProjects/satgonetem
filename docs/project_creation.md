# Project Creation

This guide covers how to build a satellite topology project entirely in Python, without
any YAML files on disk, using `SimulationProperty` and `TopologyManager.from_satcom`.

## Overview

```
SimulationProperty  (describes the constellation and ground objects)
    |
    +-- TopologyManager.from_satcom(project)
            |
            +-- satellites: {id: Satellite, ...}
            +-- ground_stations: {id: GroundStation, ...}
            +-- links: {frozenset: Link, ...}
```

`SimulationProperty` is imported directly from the `sat_com_topology` dependency
(`sat_com_builder.models`). It holds all constellation parameters in memory and
produces the configuration that `TopologyManager` needs to initialise a
`SimulationManager`. No files are required until the topology is launched.

---

## Quick start

```python
from satgonetem.utils.project_builder import create_test_project
from satgonetem.services.topology_satcom import TopologyManager

project = create_test_project()
topology = TopologyManager.from_satcom(project)

print(len(topology.satellites))       # number of satellites
print(len(topology.ground_stations))  # number of ground stations
print(len(topology.links))            # number of active links
```

`create_test_project` returns a `SimulationProperty` representing a pre-configured
Iridium-like Walker Star constellation (7 planes x 11 satellites, 86.4 deg
inclination) with five European ground stations. Use it to prototype or run tests
without writing any configuration code.

---

## Building a project from scratch

### 1. Define ground stations

```python
from satgonetem.utils.project_builder import GroundStationEntry, GroundObjectFile
from sat_com_builder.models import GroundConnectivityProperty, GroundObjectProperty

entries = [
    GroundStationEntry(index=0, name="Berlin",  latitude=52.52,  longitude=13.405, elevation_km=0.034),
    GroundStationEntry(index=1, name="London",  latitude=51.507, longitude=-0.127, elevation_km=0.011),
    GroundStationEntry(index=2, name="Madrid",  latitude=40.416, longitude=-3.703, elevation_km=0.667),
]

gs_file = GroundObjectFile("Ground Stations", entries)
data_file = gs_file.write("/tmp")

conn_props = GroundConnectivityProperty(
    ground_to_space_connections_strategy="best-angle-until-disconnection",
    elevation_above_horizon=10,
    maximum_satellite_range_distance=1500.0,
    shell_white_lists=["LEO"],
    maximum_connected_satellites=3,
)

ground_obj = GroundObjectProperty(
    identifier=gs_file.identifier,
    data_file=data_file,
    type="ground_station",
    connectivity_properties=conn_props,
)
```

If your stations are already in a file, you can also build `GroundObjectFile` from
CSV data:

```python
from satgonetem.utils.project_builder import GroundObjectFile

gs_file = GroundObjectFile.from_csv(
    identifier="Ground Stations",
    csv_path="resources/ground_station_files/representative.txt",
)
```

`from_csv` expects rows in this format (no header):
`index,name,latitude,longitude,elevation_km`.

`GroundObjectFile.identifier` becomes the key used in `ground_object_white_list` on the
shell. The identifier string must match exactly.

**Ground-to-space connection strategies**

| Strategy name                          | Description                                      |
|----------------------------------------|--------------------------------------------------|
| `best-angle-until-disconnection`       | Connect to the satellite with the best elevation angle; hold until the link drops |
| `best-range-until-disconnection`       | Connect to the nearest satellite; hold until the link drops |
| `best-multi-angle-until-disconnection` | Like best-angle but allows multiple simultaneous links |
| `longest-connection-time-strategy`     | Prefer satellites that will remain visible longest |
| `weighted-connection`                  | Score-based selection combining angle and range  |
| `everything-visible`                   | Connect to all satellites above the horizon      |
| `everything-in-range`                  | Connect to all satellites within range limit     |

### 2. Define the constellation shell

```python
from sat_com_builder.models import OrbitalConnectivityProperty, WalkerShellProperty
from sat_com_constellation.models import WalkerConstellationProperty

constellation = WalkerConstellationProperty(
    identifier="LEO",
    amount_of_orbit_plane=7,
    amount_of_satellite_per_orbit_plane=11,
    inclination=86.4,
    mean_revolution_per_day=14.35,
    phase_difference_between_satellites=True,
)

isl_props = OrbitalConnectivityProperty(
    adjacent_inter_satellite_shifting=0,
    maximum_inter_satellite_count=4,
    maximum_inter_satellite_range_distance=1500.0,
    maximum_ground_station_range=1200.0,
    maximum_user_terminal_range=1000.0,
    maximum_connected_ground_object=10000,
    maximum_connected_user_terminal=500,
    maximum_connected_ground_station=10,
)

shell = WalkerShellProperty(
    type="star",
    constellation_property=constellation,
    orbital_connectivity_property=isl_props,
    ground_object_white_list=["Ground Stations"],
)
```

`type` is either `"star"` (polar/near-polar orbits, same RAAN spacing) or `"delta"`
(inclined orbits with a delta pattern). `ground_object_white_list` must list the
identifiers of the `GroundObjectFile` objects that satellites in this shell can connect
to.

**WalkerConstellationProperty fields**

| Field | Type | Description |
|---|---|---|
| `identifier` | str | Shell name referenced by ground objects and links |
| `amount_of_orbit_plane` | int | Number of orbital planes |
| `amount_of_satellite_per_orbit_plane` | int | Satellites per plane |
| `inclination` | float | Orbital inclination in degrees |
| `mean_revolution_per_day` | float | Orbital revolutions per day |
| `phase_difference_between_satellites` | bool | Whether adjacent planes are phase-shifted |

**OrbitalConnectivityProperty fields**

| Field | Type | Default | Description |
|---|---|---|---|
| `adjacent_inter_satellite_shifting` | int | `0` | Phase offset between adjacent planes for ISLs |
| `maximum_inter_satellite_count` | int | `4` | Max ISLs per satellite |
| `maximum_inter_satellite_range_distance` | float | `None` | Max ISL distance in km |
| `maximum_ground_station_range` | float | `None` | Max ground-station link distance in km |
| `maximum_user_terminal_range` | float | `None` | Max user-terminal link distance in km |
| `maximum_connected_ground_object` | int | `None` | Max ground objects per satellite |
| `maximum_connected_user_terminal` | int | `None` | Max user terminals per satellite |
| `maximum_connected_ground_station` | int | `None` | Max ground stations per satellite |

### 3. Assemble the project

```python
from sat_com_builder.models import SimulationProperty

project = SimulationProperty(
    simulation_name="MyConstellation",
    start_date="01/01/2024 00:00:00",
    end_date="01/01/2024 00:10:00",
    walker_shells=[shell],
    ground_objects_properties=[ground_obj],
)
```

**SimulationProperty fields**

| Field | Type | Default | Description |
|---|---|---|---|
| `simulation_name` | str | required | Project name; also used as the gRPC project label |
| `start_date` | str | required | Simulation start in format `"DD/MM/YYYY HH:MM:SS"` |
| `end_date` | str | required | Simulation end in the same format |
| `walker_shells` | list | required | One or more `WalkerShellProperty` instances |
| `ground_objects_properties` | list | required | One or more `GroundObjectProperty` instances |
| `movement_model` | str | `"pyorbital"` | Orbital propagation backend |
| `distance_model` | str | `"sklearn"` | Distance computation backend |
| `disable_ground_station_link_preload` | bool | `False` | Disable link preload optimisation |
| `static_ground_station_link_mode` | bool | `False` | Freeze GSL topology after first compute |

### 4. Create the topology manager

```python
from satgonetem.services.topology_satcom import TopologyManager

topology = TopologyManager.from_satcom(project)
```

`from_satcom` calls `create_and_load_simulation` with the serialised
`SimulationProperty`, loads a `SimulationManager`, and passes it to
`TopologyManager.__init__`. No YAML file is written or read.

---

## Overriding network configuration

Build the topology with `from_satcom` first, then apply non-default network parameters
with `_apply_network_config`:

```python
from satgonetem.services.topology_satcom import TopologyManager, NetworkConfig

topology = TopologyManager.from_satcom(project)

net_cfg = NetworkConfig(
    project_name="MyConstellation",
    update_time=5,
    isl_link_capacity=100000,
    gnd_link_capacity=50000,
    routing="static",
    satellite_image="jariassuarez/sgnt:satellite",
    network_launcher="GONETEM",
    gonetem_server="localhost:10110",
)

topology._apply_network_config(net_cfg)
```

**NetworkConfig fields**

| Field | Type | Default | Description |
|---|---|---|---|
| `project_name` | str or None | `None` | Human-readable project name; falls back to simulation name |
| `update_time` | int | `5` | Topology tick interval in seconds |
| `gnd_link_capacity` | int | `100000` | Ground-station link capacity in kbps |
| `isl_link_capacity` | int | `100000` | Inter-satellite link capacity in kbps |
| `protocol` | str | `"ipv4"` | Network-layer protocol |
| `routing` | str | `"static"` | Default routing method |
| `satellite_image` | str | `"jariassuarez/sgnt:satellite"` | Docker image for satellite containers |
| `network_launcher` | str | `"GONETEM"` | Emulation backend |
| `gonetem_server` | str | `"localhost:10110"` | GoNetem gRPC server address |
| `use_budget` | bool | `False` | Enable link-budget-based capacity calculation |

---

## Saving and loading a topology

A fully configured `TopologyManager` can be serialised to a single JSON file and
restored later with `from_file`. All ground station data files referenced in the
configuration are embedded in the JSON, so the file is self-contained.

### Saving

```python
topology.to_file("my_topology.json")
```

By default `to_file` captures the current `NetworkConfig` from the instance. Pass
an explicit `NetworkConfig` to override what is persisted:

```python
from satgonetem.services.topology_satcom import NetworkConfig

net_cfg = NetworkConfig(isl_link_capacity=500000, routing="dynamic-ospf")
topology.to_file("my_topology.json", network_config=net_cfg)
```

The file contains three top-level keys:

| Key | Contents |
|---|---|
| `simulation_property` | Serialised `SimulationProperty` (constellation, ground objects, dates) |
| `network_config` | Serialised `NetworkConfig` fields |
| `ground_files` | Map of original file path to embedded file content and basename |

### Loading

```python
from satgonetem.services.topology_satcom import TopologyManager

topology = TopologyManager.from_file("my_topology.json")
```

`from_file` writes any embedded ground station data files to `/tmp/<stem>_ground_files/`
(e.g. `/tmp/my_topology_ground_files/`), updates the `data_file` paths in the
configuration, then calls `from_satcom` and
applies the persisted `NetworkConfig`. The returned instance is ready to use in the
same way as one built directly with `from_satcom`.

---

## Accessing topology data

After construction, the three main collections are ready:

```python
# Iterate satellites
for sat_id, sat in topology.satellites.items():
    print(sat_id, sat.position)   # {"latitude": ..., "longitude": ..., "altitude": ...}

# Iterate ground stations
for gs_id, gs in topology.ground_stations.items():
    print(gs_id, gs.city, gs.position)

# Iterate links
for key, link in topology.links.items():
    print(link.type, link.is_active, link.delay, link.capacity)
```

Node IDs follow these conventions:

| Node type | ID prefix | Example |
|---|---|---|
| Satellite | `"Sat"` | `"Sat42"` |
| Ground station | `"Gnd"` | `"Gnd3"` |

---

## Multiple shells

Pass multiple `WalkerShellProperty` objects to model multi-orbit constellations. Each shell
must have a distinct `identifier` in its `WalkerConstellationProperty`. Ground objects
reference shells by that identifier via `shell_white_lists` (in `GroundConnectivityProperty`)
and `ground_object_white_list` (on `WalkerShellProperty`).

```python
leo_shell = WalkerShellProperty(
    type="star",
    constellation_property=WalkerConstellationProperty(identifier="LEO", ...),
    ...
)
meo_shell = WalkerShellProperty(
    type="delta",
    constellation_property=WalkerConstellationProperty(identifier="MEO", ...),
    ...
)

conn_props = GroundConnectivityProperty(
    shell_white_lists=["LEO", "MEO"],  # ground stations can connect to both shells
    ...
)

project = SimulationProperty(
    ...,
    walker_shells=[leo_shell, meo_shell],
    ground_objects_properties=[ground_obj],
)
```

---

## User terminals

User terminals are defined the same way as ground stations but with `type="user_terminal"`:

```python
ut_entries = [
    GroundStationEntry(0, "Terminal-A", 48.8, 2.3, 0.05),
]
ut_file = GroundObjectFile("User Terminals", ut_entries)
ut_conn = GroundConnectivityProperty(
    ground_to_space_connections_strategy="best-angle-until-disconnection",
    elevation_above_horizon=5,
    maximum_satellite_range_distance=1000.0,
    shell_white_lists=["LEO"],
    maximum_connected_satellites=1,
)
ut_obj = GroundObjectProperty(
    identifier=ut_file.identifier,
    data_file=ut_file.write("/tmp"),
    type="user_terminal",
    connectivity_properties=ut_conn,
)
```

The shell's `orbital_connectivity_property.maximum_user_terminal_range` and
`maximum_connected_user_terminal` fields control how satellites treat user terminal links.
