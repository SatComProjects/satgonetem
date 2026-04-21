# Dynamics

The **dynamics** module abstracts the satellite constellation simulation engine — how nodes move, how links appear and disappear over time, and how simulation time is advanced.

`TopologyManager` inherits from the dynamics model instead of calling the simulation library directly, so the rest of the system stays library-agnostic.

## Abstract interface — `DynamicsModel`

All dynamics models inherit from `DynamicsModel` ([base.py](base.py)) and must implement:

| Method | Responsibility |
|---|---|
| `load_config()` | Load configuration (YAML, env vars, etc.) and initialise the simulation engine |
| `init()` | Populate `satellites`, `ground_stations`, and `links` from the initial simulation state |
| `check_for_updates()` | Return a `dict` with bool flags `satellites`, `ground_stations`, `links` indicating what changed |
| `update_simulation()` | Advance by one timestep: tick the clock, sync topology, trigger link add/update/delete |
| `move_to_time(new_time)` | Jump to a specific absolute `datetime` |
| `get_current_time()` | Return the current simulation time |
| `reset_simulation()` | Rewind to the start time |
| `get_current_graph()` | Return the current topology as a NetworkX graph (used for path-finding) |
| `get_satellites()` | Return `list[Satellite]` |
| `get_ground_stations()` | Return `list[GroundStation]` |

## Implementation — `SatComModel`

`SatComModel` ([satcom_model.py](satcom_model.py)) implements `DynamicsModel` using the `sat_com_application` / `sat_com_model` / `sat_com_adapter` library stack.

**What it owns:**

- Loading the simulation from `sat_com_config.yaml`, `single_parameters.yaml`, and `network_config.yaml`.
- Parallel sync of satellites, ground stations, and links from `SimulationManager` (yaspin progress spinners, `ThreadPoolExecutor`).
- Time management: `move_to_time`, `reset_simulation`, `_update_simulation_manager_time`.
- NetworkX graph construction via `NetworkXAdapter` with configurable weighting (`preference`).
- Ground-object-domain helpers: connection strategy, elevation angle, maximum connections.
- `random_link_loss` and `remove_all_ground_station_links` / `redo_all_ground_station_links`.

**Configuration overrides via `.env`** — after loading the YAML files, `load_config` checks environment variables for opt-in overrides:

| Flag | Value env var | Effect |
|---|---|---|
| `ROUTING_OVERRIDE=true` | `ROUTING_METHOD` | Replace `routing_method` from `network_config.yaml` |
| `ISL_CAPACITY_OVERRIDE=true` | `ISL_LINK_CAPACITY` | Replace `isl_link_capacity` (kbps) |
| `GSL_CAPACITY_OVERRIDE=true` | `GSL_LINK_CAPACITY` | Replace `gnd_link_capacity` (kbps) |

Copy `.env.example` (repo root) to `.env` and set the relevant flags.

## Usage — `TopologyManager`

`TopologyManager` inherits from `SatComModel`:

```python
class TopologyManager(SatComModel):
    ...
```

Methods that `SatComModel` delegates back to `TopologyManager` (called via `self`):

- `bulk_link_operations()` — executes the actual veth/tc add/update/delete via the launcher
- `_sync_interfaces_and_links()` — reconciles interface state after link changes
- `_assign_interfaces_to_nodes()`, `_set_IPs_to_nodes()`, `_add_loopback_interfaces_to_list()`
- `routing_manager.populate_routing_tables()` and related routing calls

## Creating a new dynamics model

1. Subclass `DynamicsModel`.
2. Implement all ten abstract methods.
3. Change the `TopologyManager` base class (or introduce a factory) to use your implementation.

```python
from satgonetem.dynamics.base import DynamicsModel

class MyDynamicsModel(DynamicsModel):

    def load_config(self) -> None:
        # initialise self.simulation_manager (or equivalent), routing, capacities, etc.
        ...

    def init(self) -> None:
        # populate self.satellites, self.ground_stations, self.links
        ...

    def check_for_updates(self) -> dict:
        return {'satellites': False, 'ground_stations': False, 'links': False}

    def update_simulation(self) -> None: ...
    def move_to_time(self, new_time) -> None: ...
    def get_current_time(self): ...
    def reset_simulation(self) -> None: ...
    def get_current_graph(self): ...
    def get_satellites(self) -> list: ...
    def get_ground_stations(self) -> list: ...
```
