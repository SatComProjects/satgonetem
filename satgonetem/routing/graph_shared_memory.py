import contextlib
import ipaddress
import time
from dataclasses import dataclass
from multiprocessing import shared_memory

import numpy as np

HEADER_DTYPE = np.dtype(
    [
        ("version", np.uint64),
        ("num_edges", np.int32),
        ("max_edges", np.int32),
        ("schema_version", np.int32),
    ]
)

EDGE_DTYPE = np.dtype(
    [
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
    ]
)


def ip_to_int(ip):
    return int(ipaddress.IPv4Address(ip))


@dataclass(frozen=True)
class GraphSnapshot:
    """A copy of one graph segment as read by :meth:`GraphSharedMemory.read_snapshot`.

    Attributes:
        version: Segment version at the time of the read.
        num_edges: Number of populated edge records.
        max_edges: Edge capacity advertised by the segment header.
        schema_version: Layout schema version from the header.
        edges: Copy of the populated edge records.
    """

    version: int
    num_edges: int
    max_edges: int
    schema_version: int
    edges: np.ndarray


class GraphSharedMemory:
    SCHEMA_VERSION = 1

    def __init__(self, name, max_edges, create=False):
        self.name = name
        self._owns_segment = create
        self._attach(max_edges)

    def _attach(self, max_edges):
        """Map the segment and expose it through the numpy views."""
        self.max_edges = max_edges

        self.header_size = np.zeros((1,), dtype=HEADER_DTYPE).nbytes
        self.edge_size = max_edges * np.dtype(EDGE_DTYPE).itemsize

        total_size = self.header_size + self.edge_size

        self.shm = shared_memory.SharedMemory(
            name=self.name, create=self._owns_segment, size=total_size
        )

        self.header = np.ndarray(
            (1,), dtype=HEADER_DTYPE, buffer=self.shm.buf[: self.header_size]
        )

        self.edges = np.ndarray(
            (max_edges,), dtype=EDGE_DTYPE, buffer=self.shm.buf[self.header_size :]
        )

        if self._owns_segment:
            self.header[0]["version"] = 0
            self.header[0]["num_edges"] = 0
            self.header[0]["max_edges"] = max_edges
            self.header[0]["schema_version"] = self.SCHEMA_VERSION
            self.clear()

    def ensure_capacity(self, num_edges):
        """Grow the segment so it can hold ``num_edges`` records.

        POSIX shared-memory segments cannot be resized, so the owner closes and
        unlinks the current segment and creates a larger one under the same
        name.  A consumer that already holds a mapping keeps that mapping (and
        the old data) forever; it cannot observe this recreation.  Consumers
        must re-open the segment by name for each read, for example with
        :meth:`read_snapshot`.  While the old name is unlinked and the
        replacement is being published, a re-open can raise
        ``FileNotFoundError`` or observe ``version == 0``.

        Returns:
            True when the segment was recreated, False when it already had
            enough capacity.

        Raises:
            ValueError: If this instance does not own the segment and it is
                too small. A reader cannot replace the owner's segment.
        """
        if num_edges <= self.max_edges:
            return False
        if not self._owns_segment:
            raise ValueError(
                f"Too many edges {num_edges}: "
                f"read-only segment capacity is {self.max_edges}"
            )

        new_max_edges = max(num_edges, self.max_edges * 2, 1)
        version = int(self.header[0]["version"])
        self.close()
        with contextlib.suppress(FileNotFoundError):
            self.unlink()
        self._attach(new_max_edges)
        # Keep the version monotonic for consumers that reattach.
        self.header[0]["version"] = version
        return True

    @classmethod
    def read_snapshot(
        cls,
        name,
        *,
        retries=20,
        retry_delay=0.05,
    ):
        """Read the current segment by name and size the view from its header.

        A consumer that opens the segment once keeps that mapping even after
        the owner replaces the segment under the same name, so it can never
        observe a new version or the new data.  Call this method (or re-open
        the segment by name some other way) for each read instead.  The views
        are sized from the header's ``max_edges``, so a caller's stale idea of
        the capacity cannot truncate or overrun the read.

        While the owner is recreating the segment, the name can be briefly
        absent (``FileNotFoundError``) and the replacement can briefly report
        ``version == 0``.  Both conditions are retried up to *retries* times,
        sleeping *retry_delay* seconds between attempts.  There is no
        cross-process lock, so callers should still poll ``version`` when a
        fully consistent view across multiple writes is required.

        Args:
            name: POSIX shared-memory segment name.
            retries: Number of retries after the first attempt.
            retry_delay: Seconds to sleep before each retry.

        Returns:
            A :class:`GraphSnapshot` holding a detached copy of the header
            values and of the ``num_edges`` populated records.

        Raises:
            ValueError: If *retries* is negative, the header schema is not
                supported, or the header describes a corrupt segment.
            FileNotFoundError: If the name is still absent after the retries.
            TimeoutError: If the segment still reports ``version == 0`` after
                the retries.
        """
        if retries < 0:
            raise ValueError(f"retries must be non-negative, got {retries}")

        header_size = HEADER_DTYPE.itemsize
        edge_size = np.dtype(EDGE_DTYPE).itemsize

        for attempt in range(retries + 1):
            try:
                shm = shared_memory.SharedMemory(name=name)
            except FileNotFoundError:
                if attempt == retries:
                    raise
                time.sleep(retry_delay)
                continue

            try:
                if shm.size < header_size:
                    raise ValueError(
                        f"Graph shared memory {name!r} is too small for its header"
                    )

                header = np.frombuffer(shm.buf, dtype=HEADER_DTYPE, count=1)[0].copy()
                version = int(header["version"])
                num_edges = int(header["num_edges"])
                max_edges = int(header["max_edges"])
                schema_version = int(header["schema_version"])

                if version == 0:
                    if attempt == retries:
                        raise TimeoutError(
                            f"Graph shared memory {name!r} is still at version 0 "
                            f"after {retries} retries"
                        )
                    time.sleep(retry_delay)
                    continue

                if schema_version != cls.SCHEMA_VERSION:
                    raise ValueError(
                        f"Graph shared memory {name!r} has schema version "
                        f"{schema_version}, expected {cls.SCHEMA_VERSION}"
                    )
                if max_edges < 0 or num_edges < 0 or num_edges > max_edges:
                    raise ValueError(
                        f"Graph shared memory {name!r} has an invalid header "
                        f"(num_edges={num_edges}, max_edges={max_edges})"
                    )

                expected_size = header_size + max_edges * edge_size
                if shm.size < expected_size:
                    raise ValueError(
                        f"Graph shared memory {name!r} is smaller than its header "
                        f"advertises ({shm.size} < {expected_size} bytes)"
                    )

                edges = np.ndarray(
                    (max_edges,),
                    dtype=EDGE_DTYPE,
                    buffer=shm.buf,
                    offset=header_size,
                )[:num_edges].copy()

                return GraphSnapshot(
                    version=version,
                    num_edges=num_edges,
                    max_edges=max_edges,
                    schema_version=schema_version,
                    edges=edges,
                )
            finally:
                shm.close()

        raise AssertionError("unreachable")

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
                edge["weight"],
            )

        self.header[0]["num_edges"] = num_edges
        self.header[0]["version"] += 1

    def clear(self):
        self.edges[:] = (0, 0, 0, 0, 0, 0, 0, 0, 0, -1.0)

    def close(self):
        self.shm.close()

    def unlink(self):
        self.shm.unlink()
