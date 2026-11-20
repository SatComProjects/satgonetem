## Sample Topology files

Open them with TopologyManager


```
from satgonetem.services.topology_satcom import TopologyManager


topology_manager = TopologyManager.from_file(topology_file.json)
```

Each file pins every `NetworkConfig` parameter, including the link queue
length (`link_netem_limit` and `link_txqueuelen`, in packets). Keeping the
values explicit documents the effective configuration of the sample
constellations; `0` would defer to the GoNetEm server default (1000).