# Optical ISLs, SSO shells, and inter-shell links

SatGoNetEm passes the satComTopology V5 optical and multi-shell options through
to the topology. This page covers the supported configuration, the metrics
exposed on `Link`, the temporary-unavailability policy, and the mixed
Walker/SSO example.

## Optical link model

`SimulationProperty.optical_inter_satellite_link_model` selects the model:

| Model | Behavior |
|---|---|
| `basic` (default) | Links follow the addressing plan; only distance is computed. No optical constraint is checked. |
| `advanced` | Every link is checked against its optical terminals and carries azimuth, relative azimuth, elevation, angular velocity, range, line-of-sight minimum altitude, range rate, and estimated duration. |

```python
from satgonetem.utils.project_builder import create_test_project

project = create_test_project(
    optical_inter_satellite_link_model="advanced",
)
```

Terminals are configured per shell through
`SatelliteProperty.optical_inter_satellite_link_installations`, as described in
the satComTopology builder docs. The default terminal set has four intra-plane /
inter-plane terminals and no inter-shell terminal; add an `INTER_SHELL_*`
installation when using the inter-shell strategy.

## Metrics on `Link`

`Link.optical_link_model` is `"basic"` or `"advanced"`. For advanced links,
`Link.optical_metrics` is a dict with `source` and `destination` entries. Each
entry can contain:

| Key | Meaning |
|---|---|
| `azimuth_deg` | Terminal azimuth |
| `relative_azimuth_deg` | Azimuth relative to the terminal mounting |
| `elevation_deg` | Terminal elevation |
| `angular_velocity_deg_s` | Angular tracking rate |
| `range_km` | Slant range |
| `los_min_altitude_km` | Minimum line-of-sight altitude |
| `estimated_duration_s` | Expected link duration |
| `range_rate_km_s` | Range rate |

`TopologyManager.get_link_optical_metrics(link)` returns the model and both
ends. The polling path refreshes advanced metrics each tick; `basic` links keep
`source` / `destination` as `None`.

## Temporary unavailability

SatGoNetEm implements the **hard-down** policy: when V5 marks a link
`temporary_unavailable`, the emulator brings it down and removes it from the
routing graph. When the flag clears, the link is treated as newly available
again (`to_add`), and routing reconverges.

`Link.recovery_time` carries V5's predicted recovery time. Soft impairment
(packet loss / extra delay while keeping the link up) is intentionally not
implemented at the same time; choose one policy per experiment.

This policy can cause route churn on short optical outages. Watch
`get_topology_summary()["advanced_optical_links"]` and the routing logs.

## Dawn-dusk SSO and inter-shell links

Use `create_sso_walker_project()` for a small mixed-shell example:

```python
from satgonetem.utils.project_builder import create_sso_walker_project
from satgonetem.services.topology_satcom import TopologyManager

project = create_sso_walker_project(
    walker_planes=6,
    walker_satellites_per_plane=10,
    sso_satellites=12,
)
topology = TopologyManager.from_satcom(project)

sso_shells = topology.simulation_manager.get_sso_shells()
inter_shell = [
    link for link in topology.links.values()
    if link.direction == "InterShell"
]
```

The defaults are sized for tests. Use `walker_planes=36`,
`walker_satellites_per_plane=20`, and `sso_satellites=80` to approximate the
Starlink G2 + SSO benchmark shape.

The project sets `inter_satellite_link_connection_strategy="inter-shell-sso-priority"`
and gives both shells an inter-shell-capable terminal. `INTER_SHELL` links are
mapped to the `"InterShell"` direction on the emulator `Link`.

## Limitations

- User terminals and points of presence are still not first-class emulator
  nodes. Their links are collected and ignored by the event path and are not
  built into containers. See the Phase 4 tracking item.
- Advanced optical metrics increase the per-tick cost; benchmark with
  `scripts/benchmark_topology_stages.py` before making them the default.
