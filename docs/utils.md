# Utilities and Project Building

`satgonetem.utils` is the support layer used by the service layer and by
users writing constellation scripts. It covers four broad areas:

1. **Project construction** — build `SimulationProperty` objects, ground-object
   files, custom satellites, and mesh links (`project_builder.py`).
2. **Geometry and orbital helpers** — distance, coverage, TLE generation and
   fetching (`utils.py`, `coverage.py`, `satcom_fix.py`).
3. **Compatibility** — the sat_com_topology monkey patch and the built-in
   connection strategies (`satcom_fix.py`, `custom_connection_strategies.py`).
4. **Runtime helpers** — IP math, shared-memory graph export, the interactive
   terminal bridge, and shared constants.

## Import-time behaviour

Importing `satgonetem.utils` has two deliberate side effects:

```python
# satgonetem/utils/__init__.py (simplified)
from satgonetem.utils import custom_connection_strategies  # registers strategies
from satgonetem.utils.satcom_fix import apply_satcom_fix

apply_satcom_fix()
```

- The two custom ground-to-space connection strategies are imported so their
  `@register_ground_to_space_connectivity_strategy` decorators run before any
  `SimulationManager` is created.
- `apply_satcom_fix()` replaces the orbital and distance models in
  `sat_com_trajectopy` with the local implementations, regardless of import
  order.

Because Python executes the package `__init__` before any submodule import,
importing `satgonetem.utils.project_builder` directly still applies both
patches. Importing `satgonetem.services.topology_satcom` also triggers them
through its project-builder import.

## `project_builder`

This module is the primary Python entry point for creating projects without
hand-writing YAML or sat_com JSON. It complements the narrative guide in
[Project Creation](project_creation.md); this section is the API reference.

### Ground-object files

`GroundStationEntry` is one CSV row:

```python
from satgonetem.utils.project_builder import GroundStationEntry

entry = GroundStationEntry(
    index=0,
    name="Berlin",
    latitude=52.52,
    longitude=13.405,
    elevation_km=0.034,
)
assert entry.to_csv_line() == "0,Berlin,52.52,13.405,0.034"
```

`GroundObjectFile` groups entries and writes the sat_com CSV format (no
header):

```text
index,name,latitude,longitude,elevation_km
```

```python
from satgonetem.utils.project_builder import GroundObjectFile

gs_file = GroundObjectFile(
    "Ground Stations",
    [
        GroundStationEntry(0, "Berlin", 52.52, 13.405, 0.034),
        GroundStationEntry(1, "Paris", 48.856, 2.352, 0.035),
    ],
)
path = gs_file.write("/tmp")       # /tmp/ground_stations.txt
same = GroundObjectFile.from_csv("Ground Stations", path)
```

`write()` lowercases the identifier and replaces spaces with underscores to
form the filename. `from_csv()` requires exactly five comma-separated fields
per line and raises `ValueError` otherwise; the file must not have a header.

`GroundObject` couples a file with its object type and connectivity settings
and defers file writing until `to_dict()`:

```python
from sat_com_builder.models import GroundConnectivityProperty

from satgonetem.utils.project_builder import GroundObject

ground = GroundObject(
    ground_object_file=gs_file,
    object_type="ground_station",
    connectivity_properties=GroundConnectivityProperty(
        ground_to_space_connections_strategy="best-angle-until-disconnection",
        elevation_above_horizon=10,
        maximum_satellite_range_distance=1500.0,
        shell_white_lists=["LEO"],
        maximum_connected_satellites=1,
    ),
)
config_entry = ground.to_dict("/tmp")
```

`to_dict()` writes the CSV and returns the `ground_objects_properties` entry
expected by a `SimulationProperty`.

### Building and loading simulations

| Function | Purpose |
|---|---|
| `create_and_load_simulation(dict_configuration, project_name)` | Validate a configuration dict and return an initialised `SimulationManager` |
| `create_custom_satellite(custom_sat, simulation_manager, object_id=0, domain="public")` | Add a TLE satellite; returns its `topology_uniq_id` |
| `add_custom_satellites(simulation_manager, custom_satellites)` | Add a list of `{name, tle_line1, tle_line2}` dicts |
| `create_custom_ground_station(name, lat, lon, elevation_km, object_id=0, domain="public")` | Build a fixed-position `sat_com_model` ground station |
| `add_custom_ground_station(ground_station, simulation_manager)` | Attach a ground station to a compatible domain and add it |
| `add_mesh_links(simulation_manager, ...)` | Build intra-plane, inter-plane, and ground-station links |
| `create_test_project(...)` | Build the Iridium-like test constellation |
| `create_empty_project(...)` | Build a `SimulationProperty` with no satellites or ground stations |

`create_custom_satellite()` uses the local `PyOrbitalModel`, so it works with
the patched sat_com stack. The satellite belongs to no Walker shell. After
adding it, the function refreshes ground-station links and returns the new
topology ID.

`add_custom_ground_station()` does **not** refresh automatic ground-station
links. Add every custom station first, then call:

```python
simulation_manager.update_ground_station_links()
```

Otherwise the new station participates in the topology but has no GSL.

### Mesh links

`add_mesh_links()` follows four deterministic steps:

1. Group satellites into orbital planes by RAAN. Within one RAAN cluster,
   altitude gaps larger than `max_altitude_difference` start a new sub-plane.
2. Connect each plane in mean-anomaly order: `1→2, 2→3, …, last→0`
   (`add_orbital=True`).
3. Connect consecutive planes by matching satellites in mean-anomaly order;
   the last plane does not wrap to the first (`add_adjacent=True`). Planes with
   fewer than `min_plane_size` satellites are skipped, and candidate ISLs
   longer than 4000 km are dropped.
4. Connect every ground station to its closest satellite (`add_ground=True`).

```python
from satgonetem.utils.project_builder import add_mesh_links

add_mesh_links(
    simulation_manager,
    add_orbital=True,
    add_adjacent=True,
    add_ground=True,
    max_altitude_difference=50.0,
    min_plane_size=10,
)
```

The function prints and logs the number of ISLs and GSLs created. It requires
at least two satellites; otherwise it logs a warning and returns.

### Test and empty projects

```python
from satgonetem.utils.project_builder import create_empty_project, create_test_project

project = create_test_project()                  # 7 planes × 11 satellites
empty = create_empty_project("Scratch", start_date="01/01/2026 00:00:00")
```

`create_test_project()` uses:

- a Walker **star** shell with 7 planes, 11 satellites per plane,
  86.4° inclination, 14.35 rev/day;
- five European ground stations (Berlin, London, Paris, Rome, Madrid) unless
  `ground_stations_file` points to a CSV;
- `best-angle-until-disconnection` connectivity, 10° elevation mask,
  1500 km maximum range, one connected satellite per station.

Both helpers accept `start_date` and `end_date` as `DD/MM/YYYY HH:MM:SS`.
When omitted, the start is the current UTC time and the end is ten minutes
later. `create_test_project()` writes its ground-station CSV to `/tmp`.

## `IPUtils`

`IPUtils` provides small, cached IPv4 helpers without pulling in the address
plan:

```python
from satgonetem.utils.ip_utils import IPUtils

assert IPUtils.is_valid_ipv4("10.128.0.1")
assert IPUtils.ipv4_to_binary("10.0.0.1") == "00001010000000000000000000000001"
assert IPUtils.summarize_ipv4_address("10.128.7.200", 27) == "10.128.7.192"
assert IPUtils.quaddot("00001010000000000000000000000001") == "10.0.0.1"
```

`get_ipv4_address_entropy()` returns the bit entropy of the 32-bit address,
scaled to at most 32. It is a diagnostic helper, not a routing primitive.

`is_valid_ipv4()` is intentionally permissive about leading zeros:
`"010.0.0.1"` parses as `10.0.0.1`. Use `ipaddress` when strict textual
validation matters.

## Coverage

`coverage.py` estimates constellation ground coverage from satellite
positions. Satellite objects must expose a `position` dictionary with
`latitude`, `longitude`, and `altitude` (km).

| Function | Method | Cost |
|---|---|---|
| `coverage_ring_from_elevation(...)` | Visibility ring for one satellite | Low |
| `coverage_percentage(...)` | Polygon membership on a 1° grid | High (matplotlib) |
| `coverage_percentage_fast(...)` | Spherical central-angle test, vectorised | Low |

```python
from satgonetem.utils.coverage import coverage_percentage_fast

percent = coverage_percentage_fast(
    sats=topology.get_satellites(),
    elev_min_deg=10.0,
    grid_res_deg=1.0,
)
print(f"{percent:.2f}% of the Earth's surface is covered")
```

`coverage_ring_from_elevation()` returns `(lon_deg, lat_deg, polar_flag)`.
`polar_flag=True` means the ring crosses a pole and was extended for dateline
wrapping; `(None, None, False)` means the requested elevation is geometrically
impossible at that altitude.

`coverage_percentage()` weights cells by `cos(latitude)` so polar cells do not
dominate the result. Both percentage functions include an equal-area-ish
cosine weighting, sample cell centres, and return `0.0` for an empty
constellation.

## Custom connection strategies

`custom_connection_strategies.py` registers two strategies with
sat_com_connectivity. They are referenced by string in a ground object's
`ground_to_space_connections_strategy`:

| Strategy name | Class | Behaviour |
|---|---|---|
| `longest-connection-time-strategy` | `LongestConnectionTimeStrategy` | Keeps existing visible links; otherwise picks the visible satellite with the longest predicted time until disconnection (closed-form quadratic or bisection estimator) |
| `weighted-connection` | `WeightedConnection` | Keeps existing links, then ranks visible satellites by elevation and by how few ground stations are already attached |

Both strategies retain valid existing connections first, respect
`maximum_connected_satellites`, and return a
`ComputedGroundObjectConnectionsResult` with obsolete links and new
connections.

`LongestConnectionTimeStrategy` selects the furthest satellite that is still
approaching, which minimises handovers. `WeightedConnection` uses fixed class
weights (`elevation_weight=0.5`, `connected_stations_weight=0.5`) and caps the
load term at `maximum_connected_stations=3`.

Because registration happens at import time, make sure `satgonetem.utils` has
been imported before constructing a `SimulationManager` that names one of
these strategies. The service layer always does this.

## sat_com compatibility patch

`satcom_fix.py` fixes two problems in the upstream sat_com stack:

1. **Orbital model** — `PyOrbitalModel` implements SGP4 propagation with a
   direct GMST + WGS-84 TEME→geodetic conversion. Set
   `high_precision_positions=True` to use the slower Astropy IERS transform.
2. **Distance model** — the default `SkLearningDistanceModel` methods are
   replaced with direct haversine/3D implementations. The scalar haversine is
   mathematically identical to sklearn's pairwise version but avoids per-call
   validation overhead in hot topology loops.

```python
from satgonetem.utils.satcom_fix import PyOrbitalModel, apply_satcom_fix

apply_satcom_fix()  # safe to call repeatedly
model = PyOrbitalModel(
    tle={"satellite_name": "ISS", "line1": tle1, "line2": tle2},
    time_manager=simulation_manager.time_manager,
)
lat, lon, alt_m = model.get_longitude_latitude_altitude().to_latitude_longitude_altitude()
```

The patch also rebinds `PyOrbitalModel` in
`sat_com_trajectopy.satellites.generate_satellite`, which imported the old
class by name at module import time. This is why applying the patch early
matters.

`teme_to_geodetic()` is exposed for callers that already have a TEME vector
and Julian date. `TwoLineElement` is a small Pydantic model for a single TLE.
`get_half_cone_angle()` computes the Earth central angle used by the star
constellation design.

## Graph shared memory

`GraphSharedMemory` exports a graph as a fixed-layout NumPy record array in a
POSIX shared-memory segment. It is intended for external readers that need
low-latency access to adjacency data without parsing JSON.

```python
from satgonetem.utils.graph_shared_memory import GraphSharedMemory

shm = GraphSharedMemory("satgraph", max_edges=10_000, create=True)
shm.write_edges(
    [
        {
            "src_ip": "10.0.0.4",
            "dst_ip": "10.0.0.5",
            "src_name": 0,
            "dst_name": 1,
            "src_mac": 0,
            "dst_mac": 0,
            "src_port": 0,
            "dst_port": 0,
            "flag_gateway": 0,
            "weight": 1.5,
        }
    ]
)
shm.close()
shm.unlink()  # owner only
```

The segment layout is a header (`version`, `num_edges`, `max_edges`,
`schema_version`) followed by `max_edges` records of
`EDGE_DTYPE`. `write_edges()` clears the record area, writes the edges,
updates `num_edges`, and increments `version`. `clear()` does not reset the
header. There is no cross-process locking; consumers should poll `version`.

## Interactive terminal bridge

`node_terminal_bridge.py` is a command-line helper, not an API:

```bash
python -m satgonetem.utils.node_terminal_bridge <server> <project_id> <node_name>
```

It starts a bidirectional `NodeExec` gRPC stream running `/bin/bash` with a
TTY, switches the local terminal to raw mode, forwards stdin/stdout, and
forwards `SIGWINCH` resizes. `TopologyManager.open_bash_terminal(node)` spawns
this module inside a local terminal emulator. It requires a real TTY and a
running GoNetEm project; see [Diagnostics](services.md#diagnostics) for the
manager-level entry point.

## General helpers

`utils.py` contains the small functions used across the emulator:

| Function | Behaviour |
|---|---|
| `time_(func)` | Decorator that prints `[time] <name> executed in <seconds>`; supports functions, classmethods, and staticmethods |
| `generate_satellite_tle(...)` | Build a valid TLE from inclination, mean motion, eccentricity, and angles |
| `unique_pair_id(a, b)` | Unique integer for an unordered pair; used for policy-rule IDs |
| `get_interface_from_name(list, name)` | Find an interface whose `name[3:]` matches |
| `distance_3d_km(...)` | Spherical-Earth 3D distance in km |
| `distance_3d_km_many(...)` | Vectorised equivalent for arrays |
| `fetch_iss_data()` | Fetch the live ISS TLE from `live.ariss.org` |
| `fetch_satellites_data(url)` | Parse a remote three-line-per-satellite TLE file |
| `read_satellites_data(path)` | Parse a local file in the same format |

The TLE fetchers expect groups of three non-empty lines: name, line 1, line 2.
They raise `RuntimeError` on network, format, or prefix errors rather than
returning partial data.

## Constants

`constants.py` defines two process-wide worker limits:

```python
MAX_WORKERS = os.cpu_count() or 4
ROUTE_APPLY_WORKERS = max(MAX_WORKERS, min(64, (os.cpu_count() or 4) * 2))
```

`MAX_WORKERS` is used for address application, link operations, and traffic
tools. `ROUTE_APPLY_WORKERS` is higher because route installation is
namespace-bound and benefits from more concurrency.

`test.py` in this package is an **experimental geometry scratch module**
(`geodetic_to_orbital`, ECEF/ECI helpers). It is not a test suite and is not
imported by the emulator; production orbital code lives in `satcom_fix.py`.

## Related documentation

- [Project Creation](project_creation.md)
- [Custom Project Creation](custom_project.md)
- [Data Model Layer](models.md)
- [IPv4 Addressing](addressing.md)
- [Link Budget](link_budget.md)
