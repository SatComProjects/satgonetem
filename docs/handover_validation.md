# Live handover / packet-loss validation

This page records the live Docker/GoNetEm validation for Phase 3. The goal was
to observe a ground-link handover during traffic and compare the default
break-before-make behavior with the opt-in routing-expiry penalty.

## Scenario

- GoNetEm server `v0.8.0`, `jariassuarez/sgnt:satellite` and
  `jariassuarez/sgnt:ground-station` images.
- Walker delta 4 planes x 6 satellites (24), 53 degrees inclination.
- Two ground stations, `everything-visible`, at most two GSLs per station.
- `NetworkConfig(update_time=1, routing="static")`.
- Simulation start `2024-01-01 00:00:50`; the first GSL handover occurs around
  20 s of simulation time.
- 300 ICMP echoes at 0.2 s, from the first ground station to the second.
- Baseline vs `link_expiry_horizon_s=30`, `link_expiry_penalty=100`.
- Each run starts its own GoNetEm project and cleans it up.

## Results

| Mode | GSL changes observed | Echoes received | Packet loss |
|---|---:|---:|---:|
| Baseline | 2 | 299 / 300 | 0.333 % |
| Expiry penalty | 2 | 299 / 300 | 0.333 % |

The two GSL changes were:

1. removal of the `Sat4`–`Gnd25` link;
2. addition of the `Sat9`–`Gnd24` link.

## Interpretation

- The live test confirms the emulator does perform ground-link handovers while
  traffic is running.
- One packet is lost at the break, as expected from break-before-make.
- The routing-expiry penalty did not change packet loss in this scenario: no
  alternate link was available early enough for routing to avoid the break.
  The penalty changes path selection, not link presence.

## Acceptance status

The Phase 3 acceptance criterion — *lower packet loss than break-before-make* —
is **not met**. A real make-before-break implementation would need to create or
keep the replacement link active before the old one is removed, with a traffic
overlap window. That is a separate change and was not attempted here.

The result is still useful: it establishes the baseline loss (1/300) and shows
that routing-only expiry handling is insufficient. The Phase 3 acceptance box
remains open.
