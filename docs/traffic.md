# Traffic Utilities

The `satgonetem.traffic` package provides tools for sending traffic between nodes in a running topology: **ping**, **iperf3**, **hping3**, and **FlowScheduler**. The first three follow the same pattern: a config dataclass, a results class, and a non-blocking flow class. `FlowScheduler` orchestrates many flows with bounded concurrency and wall-clock delays.

## Common pattern

Every tool exposes:

- `*Config` - a dataclass that holds all parameters and builds the shell command.
- `*Results` - parses the raw output from the tool and exposes typed properties.
- `*Flow` - wraps the call in a daemon thread so you can start it and poll.

All three are also exposed on `TopologyManager` as convenience methods. Each method accepts a single node or a list of nodes for both `source` and `destination`, starts a flow for every (source, destination) pair, and always returns a list of flow objects.

```python
flows = topology_manager.ping(src, dst)
flows = topology_manager.run_iperf3(src, dst, config)
flows = topology_manager.run_hping3(src, dst, config)
```

When you have many flows with different start times, use `FlowScheduler` instead of managing threads manually - see the [FlowScheduler](#flowscheduler) section below.

---

## ping

`ping` sends ICMP echo requests from one node to another and measures round-trip time.

### Config

```python
from satgonetem.traffic import PingConfig

config = PingConfig(
    count=5,           # number of packets (-c)
    timeout_sec=0.5,   # per-reply wait time in seconds (-W)
    interval_sec=0.2,  # interval between packets in seconds (-i)
    packet_size=56,    # payload size in bytes (-s)
)
```

### Usage

```python
import time
from satgonetem.traffic import PingFlow, PingStatus, PingConfig

flow = PingFlow(source_node, destination_node, PingConfig(count=10), delay=2.0)
flow.start()
# flow.status() == PingStatus.RUNNING during the delay period

while flow.status() == PingStatus.RUNNING:
    time.sleep(0.1)

results = flow.results()
results.print_summary()
```

The `delay` parameter (default `0.0`) tells the background thread to sleep that many seconds before executing. The flow status is `RUNNING` for the entire duration, including the delay. This lets you schedule flows relative to a common start time without blocking the calling thread.

### Results properties

| Property | Type | Description |
|---|---|---|
| `packets_transmitted` | `int` | Packets sent |
| `packets_received` | `int` | Replies received |
| `packet_loss_percent` | `float` | Loss as percentage |
| `reachable` | `bool` | True if at least one reply came back |
| `rtt_min_ms` | `float` | Minimum RTT in ms |
| `rtt_avg_ms` | `float` | Average RTT in ms |
| `rtt_max_ms` | `float` | Maximum RTT in ms |
| `rtt_mdev_ms` | `float` | RTT mean deviation in ms |

---

## iperf3

`iperf3` measures throughput between two nodes in TCP or UDP mode. One node runs a one-shot server; the other runs the client. The server exits automatically after the first connection.

### Config

```python
from satgonetem.traffic import Iperf3Config

# TCP with BBR congestion control
config = Iperf3Config(
    protocol="TCP",
    duration=10,
    bandwidth_mbps=0,         # 0 means unlimited
    parallel=1,
    interval=1.0,
    port=5201,
    congestion_control="bbr",
    window_size=None,         # e.g. "256K"
    mss=None,
    no_delay=False,
    reverse=False,
    bidir=False,
    tos=None,
    ttl=None,
    ipv6=False,
    omit=0,
    affinity=None,
)

# UDP with bandwidth cap
config = Iperf3Config(
    protocol="UDP",
    duration=10,
    bandwidth_mbps=20.0,
    pacing_timer=None,
)
```

### Usage

```python
import time
from satgonetem.traffic import Iperf3Flow, FlowStatus, Iperf3Config

config = Iperf3Config(protocol="TCP", duration=30)
flow = Iperf3Flow(source_node, destination_node, config, delay=5.0)
flow.start()
# flow.status() == FlowStatus.RUNNING during the delay period

while flow.status() == FlowStatus.RUNNING:
    time.sleep(0.5)

results = flow.results()
results.print_summary()
```

The `delay` parameter (default `0.0`) tells the background thread to sleep that many seconds before executing. The flow status is `RUNNING` for the entire duration, including the delay.

### Results properties

**Common (TCP and UDP)**

| Property | Type | Description |
|---|---|---|
| `protocol` | `str` | "TCP" or "UDP" |
| `duration_seconds` | `float` | Nominal test duration |
| `num_streams` | `int` | Number of parallel streams |
| `avg_throughput_mbps` | `float` | Mean throughput in Mbps |
| `max_throughput_mbps` | `float` | Peak interval throughput in Mbps |
| `min_throughput_mbps` | `float` | Lowest interval throughput in Mbps |
| `pmtu` | `int` | Path MTU in bytes (0 if not reported) |

**TCP only**

| Property | Type | Description |
|---|---|---|
| `total_bytes_sent` | `int` | Bytes sent |
| `total_bytes_received` | `int` | Bytes received |
| `total_retransmits` | `int` | TCP retransmissions |
| `avg_rtt_ms` | `float` | Mean RTT in ms |
| `max_rtt_ms` | `float` | Peak RTT in ms |
| `avg_rtt_var_us` | `float` | Mean RTT variance in microseconds |
| `avg_cwnd_bytes` | `float` | Mean congestion window in bytes |
| `avg_snd_wnd_bytes` | `float` | Mean send window in bytes |

**UDP only**

| Property | Type | Description |
|---|---|---|
| `total_bytes` | `int` | Bytes transmitted |
| `total_packets` | `int` | Packets transmitted |
| `avg_jitter_ms` | `float` | One-way jitter in ms |
| `total_lost_packets` | `int` | Lost packets |
| `avg_loss_percent` | `float` | Packet loss percentage |
| `total_out_of_order` | `int` | Out-of-order packets |

### DataFrames and plotting

```python
df_streams = results.get_interval_dataframe()    # per-stream interval rows
df_summary = results.get_summary_dataframe()     # aggregated interval rows

# save one metric plot to /tmp/results/
path = results.plot_metric("bits_per_second", output_dir="/tmp/results")

# save all available metric plots
paths = results.plot_all(output_dir="/tmp/results")
```

---

## hping3

`hping3` sends crafted TCP, UDP, or ICMP packets from one node to a destination IP. It is useful for measuring one-way reachability, spoofed-source tests, and basic latency profiling.

### Config

```python
from satgonetem.traffic import Hping3Config

config = Hping3Config(
    proto="tcp",           # "tcp", "udp", or "icmp"
    dport=80,              # destination port (-p), ignored for ICMP
    sport=None,            # source port override (-s)
    count=100,             # packet count (-c)
    size=0,                # extra payload bytes (-d)
    ttl=None,              # IP TTL (--ttl)
    rate_type="interval",  # "interval", "fast", "faster", or "flood"
    interval="u10000",     # used when rate_type="interval" (-i); u prefix = microseconds
    flags=["S"],           # TCP flags: "S" SYN, "A" ACK, "F" FIN, "R" RST, "P" PUSH, "U" URG
    spoof_src=None,        # explicit spoof IP (-a); falls back to node loopback when None
)
```

The `spoof_src` field controls the `-a` flag:
- When set on the config, it is used as the spoofed source address.
- When `None`, `build_command` falls back to the `bind_ip` argument (which `run_hping3` sets to `source.loopback.ipv4`).

### Usage

```python
import time
from satgonetem.traffic import Hping3Flow, Hping3Status, Hping3Config

config = Hping3Config(proto="tcp", dport=443, count=50, flags=["S"])
flow = Hping3Flow(source_node, destination_node, config, delay=1.5)
flow.start()
# flow.status() == Hping3Status.RUNNING during the delay period

while flow.status() == Hping3Status.RUNNING:
    time.sleep(0.1)

results = flow.results()
results.print_summary()
```

The `delay` parameter (default `0.0`) tells the background thread to sleep that many seconds before executing. The flow status is `RUNNING` for the entire duration, including the delay.

### Results properties

| Property | Type | Description |
|---|---|---|
| `payload_bytes` | `Optional[int]` | Per-packet payload size parsed from the header line |
| `seq` | `List[int]` | Packet sequence numbers for received replies (1-based) |
| `rtt_ms` | `List[float]` | Per-packet RTT in ms; NaN for missing replies |
| `cumulative_mbit` | `List[float]` | Cumulative bits transmitted in Mbit, one entry per reply |
| `packets_transmitted` | `Optional[int]` | Total packets sent (from summary line) |
| `packets_received` | `int` | Total replies received |
| `packet_loss_percent` | `Optional[float]` | Loss percentage (from summary line) |
| `rtt_min_ms` | `Optional[float]` | Minimum RTT in ms |
| `rtt_avg_ms` | `Optional[float]` | Average RTT in ms |
| `rtt_max_ms` | `Optional[float]` | Maximum RTT in ms |
| `reachable` | `bool` | True if at least one reply was received |

---

## TopologyManager convenience methods

When working through a `TopologyManager` instance, the same flows are available as methods. Each method accepts a single node or a list of nodes for `source` and `destination`, launches one flow per (source, destination) pair, and returns a `list` of started flow objects.

### Single-pair usage

```python
import time
from satgonetem.traffic import (
    PingConfig, PingStatus,
    Iperf3Config, FlowStatus,
    Hping3Config, Hping3Status,
)

src = topology_manager.get_node_by_name("Gnd0")
dst = topology_manager.get_node_by_name("Gnd1")

# ping - returns List[PingFlow]
flows = topology_manager.ping(src, dst, PingConfig(count=5))
for flow in flows:
    while flow.status() == PingStatus.RUNNING:
        time.sleep(0.1)
    flow.results().print_summary()

# iperf3 - returns List[Iperf3Flow]
flows = topology_manager.run_iperf3(src, dst, Iperf3Config(protocol="TCP", duration=10))
for flow in flows:
    while flow.status() == FlowStatus.RUNNING:
        time.sleep(0.5)
    flow.results().print_summary()

# hping3 - returns List[Hping3Flow]
flows = topology_manager.run_hping3(src, dst, Hping3Config(proto="tcp", dport=80, flags=["S"]))
for flow in flows:
    while flow.status() == Hping3Status.RUNNING:
        time.sleep(0.1)
    flow.results().print_summary()
```

### Multi-node usage

Pass lists of nodes to launch flows for all combinations at once. With two sources and two destinations, four flows are started.

```python
import time
from satgonetem.traffic import PingConfig, PingStatus

sources = [
    topology_manager.get_node_by_name("Gnd0"),
    topology_manager.get_node_by_name("Gnd1"),
]
destinations = [
    topology_manager.get_node_by_name("Sat0"),
    topology_manager.get_node_by_name("Sat1"),
]

flows = topology_manager.ping(sources, destinations, PingConfig(count=5))
# flows contains four PingFlow objects: (Gnd0->Sat0), (Gnd0->Sat1),
#                                        (Gnd1->Sat0), (Gnd1->Sat1)

for flow in flows:
    while flow.status() == PingStatus.RUNNING:
        time.sleep(0.1)
    flow.results().print_summary()
```

The same pattern applies to `run_iperf3` and `run_hping3`.

---

## FlowScheduler

`FlowScheduler` runs a list of flows with bounded concurrency, firing each one at the wall-clock offset stored in its `.delay` attribute. It is the recommended way to replay large traffic traces without spawning thousands of sleeping OS threads.

```python
from satgonetem.traffic import FlowScheduler, FlowSchedulerStatus
```

### Constructor

```python
scheduler = FlowScheduler(
    flows,                  # list[PingFlow | Iperf3Flow | Hping3Flow]
    max_workers=100,        # maximum flows executing at the same time
    debug=False,            # print start/done/active lines to stdout
    flow_timeout_sec=180.0, # per-flow deadline; None disables it
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `flows` | `list[AnyFlow]` | - | Flows with `.delay` set. Need not be pre-sorted. |
| `max_workers` | `int` | `100` | Thread-pool cap. Limits simultaneous executions. |
| `debug` | `bool` | `False` | Print `[flow] START/DONE/ACTIVE` lines and a progress summary every second to stdout. |
| `flow_timeout_sec` | `float or None` | `180.0` | Seconds before a running flow is declared failed. `None` disables. |

### How it works

1. On `run()`, a reference time `t0` is captured.
2. Flows are sorted by `.delay` ascending.
3. The scheduler sleeps until `t0 + flow.delay`, then submits the flow to a `ThreadPoolExecutor`.
4. Each worker zeroes `flow.delay` before calling `flow.start()` so the flow's internal sleep does not fire a second time.
5. `run()` returns immediately and executes in a background thread. Poll `status()` for completion.

### Lifecycle

| Status | Meaning |
|---|---|
| `FlowSchedulerStatus.IDLE` | Scheduler created but not yet started. |
| `FlowSchedulerStatus.RUNNING` | Flows are being scheduled and executed. |
| `FlowSchedulerStatus.DONE` | All flows have finished. |
| `FlowSchedulerStatus.ERROR` | The scheduler encountered a fatal error. |

### Usage

```python
import time
from satgonetem.traffic import PingFlow, PingConfig
from satgonetem.traffic import FlowScheduler, FlowSchedulerStatus

# Build flows with staggered delays
flows = [
    PingFlow(src, dst, PingConfig(count=5), delay=0.0),
    PingFlow(src, dst, PingConfig(count=5), delay=2.5),
    PingFlow(src, dst, PingConfig(count=5), delay=5.0),
]

scheduler = FlowScheduler(flows, max_workers=50, debug=True)
scheduler.run()

# Poll until finished
while scheduler.status() == FlowSchedulerStatus.RUNNING:
    time.sleep(0.5)

# Or block with join()
# scheduler.join()

errors = scheduler.errors()
if errors:
    for exc in errors:
        print(f"Flow failed: {exc}")
```

### Methods

| Method | Returns | Description |
|---|---|---|
| `run()` | `None` | Start the scheduler in a background thread. Raises `RuntimeError` if already started. |
| `status()` | `FlowSchedulerStatus` | Current lifecycle state. |
| `join(timeout=None)` | `None` | Block until the scheduler finishes. |
| `errors()` | `list[Exception]` | Exceptions from failed flows. Raises `RuntimeError` if still running. |
| `results(flow)` | `AnyResult` | Result of a specific flow. Raises `KeyError` if the flow failed or was not scheduled. |
