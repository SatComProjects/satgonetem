import ipaddress
from multiprocessing import shared_memory
import numpy as np

HEADER_DTYPE = np.dtype([
    ("version", np.uint64),
    ("num_edges", np.int32),
    ("max_edges", np.int32),
    ("schema_version", np.int32),
])

EDGE_DTYPE = np.dtype([
    ("src_ip", np.uint32),
    ("dst_ip", np.uint32),
    ("src_name", np.uint32),
    ("dst_name", np.uint32),
    ("src_mac", np.int64),
    ("dst_mac", np.int64),
    ("src_port", np.int32),
    ("dst_port", np.int32),
    ("flag_gateway", np.int32),
    ("weight", np.float32),
])


def ip_to_int(ip):
    return int(ipaddress.IPv4Address(ip))


class GraphSharedMemory:
    SCHEMA_VERSION = 1

    def __init__(self, name, max_edges, create=False):

        self.max_edges = max_edges

        self.header_size = np.zeros((1,), dtype=HEADER_DTYPE).nbytes
        self.edge_size = max_edges * np.dtype(EDGE_DTYPE).itemsize

        total_size = self.header_size + self.edge_size

        self.shm = shared_memory.SharedMemory(
            name=name,
            create=create,
            size=total_size
        )

        self.header = np.ndarray(
            (1,),
            dtype=HEADER_DTYPE,
            buffer=self.shm.buf[:self.header_size]
        )

        self.edges = np.ndarray(
            (max_edges,),
            dtype=EDGE_DTYPE,
            buffer=self.shm.buf[self.header_size:]
        )

        if create:
            self.header[0]["version"] = 0
            self.header[0]["num_edges"] = 0
            self.header[0]["max_edges"] = max_edges
            self.header[0]["schema_version"] = self.SCHEMA_VERSION
            self.clear()

    def write_edges(self, edge_list):

        num_edges = len(edge_list)

        if num_edges > self.max_edges:
            raise ValueError(f"Too many edges {num_edges}")

        self.clear()

        for i, edge in enumerate(edge_list):
            self.edges[i] = (
                ip_to_int(edge["src_ip"]),
                ip_to_int(edge["dst_ip"]),
                edge["src_name"],
                edge["dst_name"],
                edge["src_mac"],
                edge["dst_mac"],
                edge["src_port"],
                edge["dst_port"],
                edge["flag_gateway"],
                edge["weight"]
            )

        self.header[0]["num_edges"] = num_edges
        self.header[0]["version"] += 1

    def clear(self):
        self.edges[:] = (0, 0, 0, 0, 0, 0, 0, 0, 0, -1.0)

    def close(self):
        self.shm.close()

    def unlink(self):
        self.shm.unlink()
