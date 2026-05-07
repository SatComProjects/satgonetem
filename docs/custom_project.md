# Custom Project Creation

This guide explains how to build a satellite topology project using the
imperative convenience API in `satgonetem.utils.project_builder`.  Instead of
assembling a large `SimulationProperty` up front, you can start from an empty
project and incrementally add satellites, ground stations, and links exactly how
you want.

## When to use this approach

* You want a **blank slate** with no default constellation.
* You have **real TLEs** for individual satellites rather than Walker-shell
  parameters.
* You need to **mix** a generated Walker constellation with custom satellites
  or ground stations.
* You want to **manually wire** inter-satellite or ground links rather than
  relying entirely on automatic connectivity.

---

## Starting from an empty project

`create_empty_project` returns a `SimulationProperty` that contains **no**
satellites, shells, or ground stations—only a simulation name and time window.

```python
from satgonetem.utils.project_builder import create_empty_project

project = create_empty_project(
    simulation_name="MyCustomProject",
    start_date="01/01/2024 00:00:00",
    end_date="01/01/2024 01:00:00",
)
```

If `start_date` or `end_date` are omitted, they default to the current UTC time
and 10 minutes later, respectively.

---

## Loading a SimulationManager

Before you can add custom objects you need a `SimulationManager`.  Use
`create_and_load_simulation` to turn any `SimulationProperty` dict into one:

```python
from satgonetem.utils.project_builder import create_and_load_simulation

sim_manager = create_and_load_simulation(
    dict_configuration=project.model_dump(),
    project_name=project.simulation_name,
)
```

You now have a live `SimulationManager` that you can modify programmatically.

---

## Adding custom satellites

### Single satellite

`create_custom_satellite` builds a `PyOrbitalModel` from a TLE dictionary and
injects it into the manager.  Ground-station links are refreshed automatically
so the new satellite is considered for connections.

```python
from satgonetem.utils.project_builder import create_custom_satellite

custom_sat = {
    "name": "ISS (ZARYA)",
    "tle_line1": "1 25544U 98067A   26126.19956580  .00006859  00000-0  13198-3 0  9993",
    "tle_line2": "2 25544  51.6304 147.9377 0007403  28.6982 331.4412 15.49115842565248",
}

new_id = create_custom_satellite(custom_sat, sim_manager)
print(f"Added satellite with topology id {new_id}")
```

The function returns the new satellite's `topology_uniq_id` as an `int`.

### Multiple satellites

Use `add_custom_satellites` to bulk-inject a list:

```python
from satgonetem.utils.project_builder import add_custom_satellites

sats = [
    {"name": "Sat-A", "tle_line1": "...", "tle_line2": "..."},
    {"name": "Sat-B", "tle_line1": "...", "tle_line2": "..."},
]
add_custom_satellites(sim_manager, sats)
```

### Fetching live ISS data

A small helper fetches the current ISS TLE from the web:

```python
from satgonetem.utils.utils import fetch_iss_data
from satgonetem.utils.project_builder import create_custom_satellite

iss = fetch_iss_data()
create_custom_satellite(iss, sim_manager)
```

### Fetching multiple satellites from a URL

If you have a URL that serves TLE data for many satellites (three lines per
satellite: name, line 1, line 2), use `fetch_satellites_data`:

```python
from satgonetem.utils.utils import fetch_satellites_data
from satgonetem.utils.project_builder import add_custom_satellites

satellites = fetch_satellites_data("https://example.com/constellation.txt")
add_custom_satellites(sim_manager, satellites)
```

The returned list has the same shape used by `add_custom_satellites`:

```python
[
    {
        "name": "ONEWEB-0012",
        "tle_line1": "1 44057U 19010A   26126.83484127  .00000046  00000+0  86073-4 0  9992",
        "tle_line2": "2 44057  87.9096 236.9957 0002389 103.2080 256.9318 13.16595320346123",
    },
    ...
]
```

### Reading multiple satellites from a local file

For offline workflows you can read the same three-line format from a local
`.txt` file with `read_satellites_data`:

```python
from satgonetem.utils.utils import read_satellites_data
from satgonetem.utils.project_builder import add_custom_satellites

satellites = read_satellites_data("./tle_data/oneweb.txt")
add_custom_satellites(sim_manager, satellites)
```

---

## Adding custom ground stations

### Create a ground station

`create_custom_ground_station` builds a fixed-position ground station:

```python
from satgonetem.utils.project_builder import create_custom_ground_station

gs = create_custom_ground_station(
    name="CustomGS",
    latitude=48.856,
    longitude=2.352,
    elevation_km=0.035,
    object_id=100,
    domain="public",
)
```

| Parameter | Description |
|---|---|
| `name` | Human-readable label (e.g. city name). |
| `latitude` / `longitude` | Decimal degrees. |
| `elevation_km` | Elevation above sea level in kilometres. |
| `object_id` | Business identifier (defaults to `0`). |
| `domain` | Domain identifier (defaults to `"public"`). |

### Register it in the simulation

```python
from satgonetem.utils.project_builder import add_custom_ground_station

add_custom_ground_station(gs, sim_manager)
```

**Note:** automatic ground-station links are **not** refreshed by this helper.
If you want the manager to connect the station to visible satellites, set a
`ground_object_domain` on the station and call
`simulation_manager.update_ground_station_links()` after all stations have been
added.

---

## Creating manual links

Links are created directly through the `SimulationManager`.  Both endpoints must
already exist in the manager.

### Inter-satellite link (ISL)

```python
sat_a = next(s for s in sim_manager.get_satellites() if s.satellite_name == "SatA")
sat_b = next(s for s in sim_manager.get_satellites() if s.satellite_name == "SatB")
sim_manager.create_and_add_inter_satellite_link_connection(sat_a, sat_b)
```

### Ground-station link (GSL)

```python
sat = next(s for s in sim_manager.get_satellites() if s.satellite_name == "SatA")
gs = next(g for g in sim_manager.get_ground_stations() if g.label == "CustomGS")
sim_manager.create_and_add_ground_station_link_connection(sat, gs)
```

### Ground-object link

Connects any two ground objects (ground stations, user terminals, or points of
presence) by their `label`:

```python
go_a = next(g for g in sim_manager.get_all_ground_objects() if g.label == "CustomGS")
go_b = next(g for g in sim_manager.get_all_ground_objects() if g.label == "AnotherGS")
sim_manager.create_and_add_ground_object_link_connection(go_a, go_b)
```

### Automatic mesh links

If you have loaded a set of real TLE-based satellites (e.g. from Celestrak) and
want a Walker-delta-style mesh without manually wiring every pair, use
`add_mesh_links`.  It automatically:

1. **Groups satellites into orbital planes** by clustering their TLE **RAAN**
   values.  Satellites whose altitude differs by more than
   *max_altitude_difference* from the rest of their RAAN cluster are placed in
   separate sub-planes (this prevents deorbiting or misplaced satellites from
   being meshed with the main constellation).
2. **Intra-plane links** – within each plane satellites are sorted by their
   *actual propagated position* (argument of latitude) and connected
   sequentially (1→2, 2→3, …, last→0).
3. **Inter-plane links** – for each pair of consecutive planes, satellites are
   matched by index (satellite j in plane A connects to satellite j in plane B).
   Links longer than **4000 km** are skipped, and planes with fewer than
   *min_plane_size* satellites do not participate in inter-plane connections.
4. **Ground links** – each ground station is connected to the closest satellite.

```python
from satgonetem.utils.project_builder import add_mesh_links

add_mesh_links(sim_manager)
```

This produces a topology where most satellites have **two to four** ISLs
(two intra-plane, plus up to two inter-plane).  Deviations occur when orbital
planes contain different numbers of satellites (common with real TLE data where
some satellites have drifted or de-orbited) or when adjacent planes are too far
apart in RAAN.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `simulation_manager` | `SimulationManager` | — | The manager to wire links into. |
| `add_orbital` | `bool` | `True` | Create intra-plane (ORBITAL) ISLs. |
| `add_adjacent` | `bool` | `True` | Create inter-plane (ADJACENT) ISLs. |
| `add_ground` | `bool` | `True` | Connect ground stations to the closest satellite. |
| `max_altitude_difference` | `float` | `50.0` | Maximum altitude difference (km) between satellites in the same plane. |
| `min_plane_size` | `int` | `10` | Minimum plane size to participate in inter-plane links. |

---

## Building the TopologyManager

All modifications to the `SimulationManager` must happen **before** the
`TopologyManager` is created, because the topology manager performs an initial
sync that reads satellites, ground stations, and links from the manager.

```python
from satgonetem.services.topology_satcom import TopologyManager

topology = TopologyManager.from_simulation_manager(sim_manager)
```

If you prefer to start from a `SimulationProperty` directly and skip manual
injection, use `TopologyManager.from_satcom(project)` instead.

---

## Complete examples

### Example 1: TLE-only constellation (no Walker shells)

```python
from satgonetem.utils.project_builder import (
    create_empty_project,
    create_and_load_simulation,
    create_custom_satellite,
)
from satgonetem.services.topology_satcom import TopologyManager

# 1. Empty project
project = create_empty_project(
    simulation_name="TLEOnly",
    start_date="01/01/2024 00:00:00",
    end_date="01/01/2024 01:00:00",
)

# 2. Load manager
sim_manager = create_and_load_simulation(
    project.model_dump(), project.simulation_name
)

# 3. Add custom satellites
tle_sats = [
    {"name": "Sat1", "tle_line1": "1 00001U ...", "tle_line2": "2 00001 ..."},
    {"name": "Sat2", "tle_line1": "1 00002U ...", "tle_line2": "2 00002 ..."},
    {"name": "Sat3", "tle_line1": "1 00003U ...", "tle_line2": "2 00003 ..."},
]
for s in tle_sats:
    create_custom_satellite(s, sim_manager)

# 4. Manually wire ISLs
sat1 = next(s for s in sim_manager.get_satellites() if s.satellite_name == "Sat1")
sat2 = next(s for s in sim_manager.get_satellites() if s.satellite_name == "Sat2")
sat3 = next(s for s in sim_manager.get_satellites() if s.satellite_name == "Sat3")
sim_manager.create_and_add_inter_satellite_link_connection(sat1, sat2)
sim_manager.create_and_add_inter_satellite_link_connection(sat2, sat3)

# 5. Build topology
topology = TopologyManager.from_simulation_manager(sim_manager)
```

### Example 2: Walker shell + custom ISS + custom ground station

```python
from satgonetem.utils.project_builder import (
    create_test_project,
    create_and_load_simulation,
    create_custom_satellite,
    create_custom_ground_station,
    add_custom_ground_station,
)
from satgonetem.services.topology_satcom import TopologyManager
from satgonetem.utils.utils import fetch_iss_data

# 1. Start with the default test constellation
project = create_test_project(simulation_name="Hybrid")

# 2. Load manager
sim_manager = create_and_load_simulation(
    project.model_dump(), project.simulation_name
)

# 3. Inject the live ISS
iss = fetch_iss_data()
create_custom_satellite(iss, sim_manager)

# 4. Add a custom ground station
gs = create_custom_ground_station("Paris", 48.856, 2.352, 0.035)
add_custom_ground_station(gs, sim_manager)
sim_manager.update_ground_station_links()

# 5. Force a manual link between ISS and Paris
iss_sat = next(s for s in sim_manager.get_satellites() if s.satellite_name == "ISS (ZARYA)")
paris_gs = next(g for g in sim_manager.get_ground_stations() if g.label == "Paris")
sim_manager.create_and_add_ground_station_link_connection(iss_sat, paris_gs)

# 6. Build topology
topology = TopologyManager.from_simulation_manager(sim_manager)
```

### Example 3: Mixed LEO + MEO with custom cross-shell links

```python
from sat_com_builder.models import SimulationProperty, GroundConnectivityProperty
from sat_com_constellation.models import WalkerConstellationProperty
from sat_com_builder.models import OrbitalConnectivityProperty, WalkerShellProperty
from satgonetem.utils.project_builder import (
    create_and_load_simulation,
    create_custom_satellite,
)
from satgonetem.services.topology_satcom import TopologyManager

leo = WalkerShellProperty(
    type="star",
    constellation_property=WalkerConstellationProperty(
        identifier="LEO", amount_of_orbit_plane=6,
        amount_of_satellite_per_orbit_plane=10,
        inclination=53.0, mean_revolution_per_day=15.0,
        phase_difference_between_satellites=True,
    ),
    orbital_connectivity_property=OrbitalConnectivityProperty(
        maximum_inter_satellite_count=4,
        maximum_inter_satellite_range_distance=2000.0,
    ),
    ground_object_white_list=[],
)

meo = WalkerShellProperty(
    type="delta",
    constellation_property=WalkerConstellationProperty(
        identifier="MEO", amount_of_orbit_plane=2,
        amount_of_satellite_per_orbit_plane=5,
        inclination=55.0, mean_revolution_per_day=6.0,
        phase_difference_between_satellites=False,
    ),
    orbital_connectivity_property=OrbitalConnectivityProperty(
        maximum_inter_satellite_count=2,
        maximum_inter_satellite_range_distance=5000.0,
    ),
    ground_object_white_list=[],
)

project = SimulationProperty(
    simulation_name="MixedOrbits",
    start_date="01/01/2024 00:00:00",
    end_date="01/01/2024 02:00:00",
    walker_shells=[leo, meo],
    ground_objects_properties=[],
)

sim_manager = create_and_load_simulation(
    project.model_dump(), project.simulation_name
)

# Add a custom relay satellite that bridges both shells
custom = {
    "name": "Relay-1",
    "tle_line1": "1 99999U ...",
    "tle_line2": "2 99999 ...",
}
create_custom_satellite(custom, sim_manager)

# Manually link the relay to one satellite from each shell
# (Walker names are generated automatically: "LEO 0", "LEO 1", ...)
relay = next(s for s in sim_manager.get_satellites() if s.satellite_name == "Relay-1")
leo_0 = next(s for s in sim_manager.get_satellites() if s.satellite_name == "LEO 0")
meo_0 = next(s for s in sim_manager.get_satellites() if s.satellite_name == "MEO 0")
sim_manager.create_and_add_inter_satellite_link_connection(relay, leo_0)
sim_manager.create_and_add_inter_satellite_link_connection(relay, meo_0)

topology = TopologyManager.from_simulation_manager(sim_manager)
```

---

## Using `SimulationProperty` directly (declarative alternative)

If you prefer a declarative style but still want to build from Python, assemble a
`SimulationProperty` directly from `sat_com_builder.models`.  Use `GroundObjectFile`
to write ground-station CSVs, then pass the resulting path to
`GroundObjectProperty`:

```python
from sat_com_builder.models import (
    GroundConnectivityProperty,
    GroundObjectProperty,
    OrbitalConnectivityProperty,
    SimulationProperty,
    WalkerShellProperty,
)
from sat_com_constellation.models import WalkerConstellationProperty
from satgonetem.utils.project_builder import GroundStationEntry, GroundObjectFile

# Ground objects
entries = [
    GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034),
    GroundStationEntry(1, "London", 51.507, -0.127, 0.011),
]
gs_file = GroundObjectFile("Ground Stations", entries)
gs_path = gs_file.write("/tmp")

conn = GroundConnectivityProperty(
    ground_to_space_connections_strategy="best-angle-until-disconnection",
    elevation_above_horizon=10,
    maximum_satellite_range_distance=1500.0,
    shell_white_lists=["LEO"],
    maximum_connected_satellites=3,
)
gs_obj = GroundObjectProperty(
    identifier="Ground Stations",
    data_file=gs_path,
    type="ground_station",
    connectivity_properties=conn,
)

# Shell
shell = WalkerShellProperty(
    type="star",
    constellation_property=WalkerConstellationProperty(
        identifier="LEO", amount_of_orbit_plane=7,
        amount_of_satellite_per_orbit_plane=11,
        inclination=86.4, mean_revolution_per_day=14.35,
        phase_difference_between_satellites=True,
    ),
    orbital_connectivity_property=OrbitalConnectivityProperty(
        maximum_inter_satellite_count=4,
        maximum_inter_satellite_range_distance=1500.0,
    ),
    ground_object_white_list=["Ground Stations"],
)

# Assemble
project = SimulationProperty(
    simulation_name="BuilderDemo",
    start_date="01/01/2024 00:00:00",
    end_date="01/01/2024 00:10:00",
    walker_shells=[shell],
    ground_objects_properties=[gs_obj],
)

sim_manager = create_and_load_simulation(project.model_dump(), project.simulation_name)
topology = TopologyManager.from_simulation_manager(sim_manager)
```

---

## Saving and reloading

Once you have a `TopologyManager`, you can persist the entire state to a single
JSON file and restore it later.  This works for **both** declarative
(`from_satcom`) and programmatic (`from_simulation_manager`) workflows.

```python
from satgonetem.services.topology_satcom import NetworkConfig

net_cfg = NetworkConfig(
    project_name="CustomProject",
    update_time=5,
    isl_link_capacity=100_000,
    gnd_link_capacity=50_000,
    routing="static",
    gonetem_server="localhost:10110",
)

topology.to_file("my_custom_project.json", network_config=net_cfg)
```

Restore it later:

```python
topology = TopologyManager.from_file("my_custom_project.json")
```

---

## Summary of key functions

| Function | Purpose |
|---|---|
| `create_empty_project` | Blank `SimulationProperty` with no shells or ground objects. |
| `create_and_load_simulation` | Turn a configuration dict into a `SimulationManager`. |
| `create_custom_satellite` | Add a single TLE-based satellite to a manager. |
| `add_custom_satellites` | Bulk-add TLE-based satellites. |
| `create_custom_ground_station` | Build a fixed-position ground station object. |
| `add_custom_ground_station` | Register a ground station in a manager. |
| `create_custom_user_terminal` | Build a fixed-position user terminal object. |
| `create_custom_point_of_presence` | Build a fixed-position point of presence object. |
| `add_mesh_links` | Auto-create a Walker-delta-style mesh from TLE-based satellites and connect ground stations to the closest satellite. |
| `sim_manager.create_and_add_inter_satellite_link_connection(...)` | Wire two satellites directly via the `SimulationManager`. |
| `sim_manager.create_and_add_ground_station_link_connection(...)` | Wire a satellite to a ground station directly via the `SimulationManager`. |
| `sim_manager.create_and_add_ground_object_link_connection(...)` | Wire two ground objects directly via the `SimulationManager`. |
| `fetch_iss_data` | Fetch the current ISS TLE from the web. |
| `fetch_satellites_data` | Fetch a list of satellite TLEs from a URL. |
| `read_satellites_data` | Read a list of satellite TLEs from a local file. |

