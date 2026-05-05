"""Bounded concurrency scheduler for satgonetem flow replay.

Replaces the naive pattern of starting one OS thread per flow. Instead a
single scheduler thread submits work to a ThreadPoolExecutor when each
flow's deadline arrives. Pool size caps the number of flows executing at the
same time, eliminating the overhead of thousands of sleeping threads.

Typical usage::

    flows = flows_from_cicflowmeter(path, nodes, nodes, sample_fraction=0.1)
    scheduler = FlowScheduler(flows, max_workers=100)
    scheduler.run()
    while scheduler.status() == FlowSchedulerStatus.RUNNING:
        time.sleep(0.5)
    errors = scheduler.errors()
"""

from __future__ import annotations

import enum
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Lock
from typing import TYPE_CHECKING, Callable, Sequence, Union

if TYPE_CHECKING:
    from satgonetem.traffic.hping3_utils import Hping3Flow, Hping3Results
    from satgonetem.traffic.iperf3_utils import Iperf3Flow, Iperf3Results
    from satgonetem.traffic.ping_utils import PingFlow, PingResults

AnyFlow = Union["Iperf3Flow", "Hping3Flow", "PingFlow"]
AnyResult = Union["Iperf3Results", "Hping3Results", "PingResults"]


class FlowSchedulerStatus(enum.Enum):
    """Lifecycle state of a FlowScheduler.

    Attributes:
        IDLE: Scheduler created but not yet started.
        RUNNING: Flows are being scheduled and executed.
        DONE: All flows have finished executing.
        ERROR: The scheduler itself encountered a fatal error.
    """

    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


def _flow_label(flow: AnyFlow) -> str:
    """Build a stable, human-readable flow label for console output.

    Args:
        flow: Flow instance to describe.

    Returns:
        A string like "Iperf3Flow Gnd1 -> Gnd2".
    """
    kind = type(flow).__name__
    src = flow.source.name
    dst = flow.destination.name
    return f"{kind} {src} -> {dst}"


def _execute_flow(
    flow: AnyFlow,
    debug: bool = False,
    on_start: Callable[[AnyFlow], None] | None = None,
    on_done: Callable[[AnyFlow], None] | None = None,
    join_timeout_sec: float | None = None,
) -> AnyResult:
    """Run one flow synchronously inside a pool worker thread.

    Zeros flow.delay before starting so the flow's internal sleep does not
    fire a second time (the scheduler already waited for the right wall-clock
    moment). Then blocks via flow._thread.join() until the flow finishes.

    Args:
        flow: The flow to execute. Must be in IDLE state.
        debug: If True, print start/done messages for this flow.
        on_start: Optional callback invoked when flow execution starts.
        on_done: Optional callback invoked when flow execution finishes.
        join_timeout_sec: Maximum seconds to wait for the flow thread to
            complete. When exceeded, raises TimeoutError.

    Returns:
        The parsed results from the flow (Iperf3Results, Hping3Results, or
        PingResults depending on the flow type).

    Raises:
        TimeoutError: If the flow thread does not complete within
            join_timeout_sec.
    """
    label = _flow_label(flow)
    if debug:
        print(f"[flow] START {label}")
    if on_start is not None:
        on_start(flow)
    try:
        flow.delay = 0.0
        flow.start()
        if flow._thread is not None:
            flow._thread.join(timeout=join_timeout_sec)
            if flow._thread.is_alive():
                raise TimeoutError(f"Flow timed out after {join_timeout_sec}s: {label}")
        if debug:
            print(f"[flow] DONE  {label}")
    except Exception as exc:
        if debug:
            print(f"[flow] FAIL  {label}: {exc}")
        raise
    finally:
        if on_done is not None:
            on_done(flow)
    return flow.results()


class FlowScheduler:
    """Time-ordered flow replay with bounded concurrency.

    Reads each flow's .delay attribute as an offset in seconds from the
    moment run() is called. Submits work to a ThreadPoolExecutor at the
    right wall-clock time so that at most max_workers flows execute
    concurrently. If all workers are busy when a deadline arrives the flow
    is queued and picked up as soon as a worker frees.

    run() is non-blocking; use status() to poll for completion and errors()
    to retrieve the list of failed flows once finished.

    Attributes:
        _flows: Flows sorted ascending by delay.
        _max_workers: Maximum number of flows executing concurrently.
        _results: Ordered list of flows that completed successfully.
        _result_map: Maps flow object id to its completed flow for O(1) lookup.
    """

    def __init__(
        self,
        flows: Sequence[AnyFlow],
        max_workers: int = 100,
        debug: bool = False,
        flow_timeout_sec: float | None = 180.0,
    ) -> None:
        """
        Args:
            flows: Any sequence of flow objects with .delay set (e.g. from
                flows_from_cicflowmeter). Need not be pre-sorted.
            max_workers: Maximum concurrent flows. Defaults to 100.
            debug: If True, print a line to stdout when each flow starts
                and finishes, plus snapshots of currently active flows.
            flow_timeout_sec: Maximum seconds to wait for each flow to
                complete before marking it failed. Set to None to disable
                timeout. Defaults to 180.0.

        Raises:
            ValueError: If max_workers is less than 1.
        """
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1, got {max_workers}")
        if flow_timeout_sec is not None and flow_timeout_sec <= 0:
            raise ValueError(
                "flow_timeout_sec must be > 0 when provided, " f"got {flow_timeout_sec}"
            )
        self._flows = sorted(flows, key=lambda f: f.delay)
        self._max_workers = max_workers
        self._debug = debug
        self._flow_timeout_sec = flow_timeout_sec
        self._active_flows: dict[str, int] = {}
        self._active_flows_lock = Lock()
        self._results: list[AnyResult] = []
        self._result_map: dict[int, AnyResult] = {}
        self._total_count = len(flows)
        self._completed_count = 0
        self._status = FlowSchedulerStatus.IDLE
        self._status_lock = Lock()
        self._errors: list[Exception] = []
        self._scheduler_error: Exception | None = None
        self._scheduler_thread: threading.Thread | None = None

    def _on_flow_start(self, flow: AnyFlow) -> None:
        """Register flow start.

        Args:
            flow: Flow that has started executing.
        """
        label = _flow_label(flow)
        with self._active_flows_lock:
            self._active_flows[label] = self._active_flows.get(label, 0) + 1

    def _on_flow_done(self, flow: AnyFlow) -> None:
        """Register flow completion.

        Args:
            flow: Flow that has finished executing.
        """
        label = _flow_label(flow)
        with self._active_flows_lock:
            self._completed_count += 1
            count = self._active_flows.get(label, 0)
            if count <= 1:
                self._active_flows.pop(label, None)
            else:
                self._active_flows[label] = count - 1

    def _print_progress(self) -> None:
        """Print periodic progress summary to stdout."""
        with self._active_flows_lock:
            active = sum(self._active_flows.values())
            completed = self._completed_count
            total = self._total_count
        print("=" * 40)
        print(f"FlowScheduler: {active} in process | {completed}/{total} completed")
        print("=" * 40)

    def run(self) -> None:
        """Start executing all flows in delay order with bounded concurrency.

        Returns immediately; poll status() for completion. The method submits
        each flow to the internal ThreadPoolExecutor at (t0 + flow.delay)
        wall-clock time, where t0 is the instant run() is called.
        Successful results are stored and accessible via results().

        Raises:
            RuntimeError: If the scheduler has already been started.
        """
        with self._status_lock:
            if self._status != FlowSchedulerStatus.IDLE:
                raise RuntimeError(
                    f"FlowScheduler has already been started (status={self._status.value})"
                )
            self._status = FlowSchedulerStatus.RUNNING
            self._errors = []
            self._results = []
            self._result_map = {}
            self._completed_count = 0
            self._active_flows.clear()
            self._scheduler_error = None

        self._scheduler_thread = threading.Thread(target=self._run, daemon=True)
        self._scheduler_thread.start()

    def _run(self) -> None:
        """Internal scheduling loop executed in a background thread."""
        t0 = time.monotonic()
        errors: list[Exception] = []

        progress_event = threading.Event()

        def _progress_printer() -> None:
            while not progress_event.is_set():
                progress_event.wait(1.0)
                if not progress_event.is_set():
                    self._print_progress()

        try:
            with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                if self._debug:
                    progress_thread = threading.Thread(
                        target=_progress_printer, daemon=True
                    )
                    progress_thread.start()

                flow_futures: list[tuple[AnyFlow, Future[AnyResult]]] = []
                for flow in self._flows:
                    wait = (t0 + flow.delay) - time.monotonic()
                    if wait > 0.0:
                        time.sleep(wait)
                    future: Future[AnyResult] = executor.submit(
                        _execute_flow,
                        flow,
                        False,
                        self._on_flow_start if self._debug else None,
                        self._on_flow_done if self._debug else None,
                        self._flow_timeout_sec,
                    )
                    flow_futures.append((flow, future))

                for flow, future in flow_futures:
                    exc = future.exception()
                    if exc is not None:
                        if isinstance(exc, Exception):
                            errors.append(exc)
                        else:
                            errors.append(
                                RuntimeError(
                                    f"Flow failed with {type(exc).__name__}: {exc}"
                                )
                            )
                    else:
                        completed = future.result()
                        self._results.append(completed)
                        self._result_map[id(flow)] = completed

                if self._debug:
                    progress_event.set()
                    progress_thread.join(timeout=2.0)
        except Exception as exc:
            with self._status_lock:
                self._scheduler_error = exc
                self._status = FlowSchedulerStatus.ERROR
        else:
            with self._status_lock:
                self._errors = errors
                self._status = FlowSchedulerStatus.DONE

    def status(self) -> FlowSchedulerStatus:
        """Return the current lifecycle state.

        Returns:
            The current FlowSchedulerStatus value.
        """
        with self._status_lock:
            return self._status

    def errors(self) -> list[Exception]:
        """Return the list of exceptions raised by failed flows.

        Returns:
            A list of exceptions raised by flows that failed. Empty when all
            flows complete successfully.

        Raises:
            RuntimeError: If the scheduler is still running or encountered a
                fatal error.
        """
        with self._status_lock:
            if self._status == FlowSchedulerStatus.RUNNING:
                raise RuntimeError("FlowScheduler is still running")
            if self._status == FlowSchedulerStatus.ERROR:
                raise RuntimeError(
                    f"FlowScheduler failed: {self._scheduler_error}"
                ) from self._scheduler_error
            return self._errors

    def join(self, timeout: float | None = None) -> None:
        """Wait for the scheduler thread to finish.

        Args:
            timeout: Maximum seconds to wait. None means wait indefinitely.
        """
        if self._scheduler_thread is not None:
            self._scheduler_thread.join(timeout=timeout)

    def results(self, flow: AnyFlow) -> AnyResult:
        """Return the result of a specific flow after run() has completed.

        Args:
            flow: The flow whose result to retrieve. Must have completed
                successfully during the last run() call.

        Returns:
            The flow object after execution, with any result attributes
            populated by the underlying tool (iperf3, hping3, ping, etc.).

        Raises:
            RuntimeError: If the scheduler is still running.
            KeyError: If the flow did not complete successfully, was not part
                of this scheduler, or run() has not been called yet.
        """
        with self._status_lock:
            if self._status == FlowSchedulerStatus.RUNNING:
                raise RuntimeError("FlowScheduler is still running")
        try:
            return self._result_map[id(flow)]
        except KeyError:
            label = _flow_label(flow)
            raise KeyError(
                f"No result found for flow {label!r}. "
                "The flow may have failed, was not scheduled, or run() has not been called."
            ) from None
