"""Process isolation for the native GNU Radio gr-uhd B210 flowgraph.

Some UHD/libusb disconnect paths terminate the process from a native worker
thread instead of raising a Python exception.  The public adapter must survive
that failure so it can publish the required typed terminal events.  This
module therefore keeps the programmatic :class:`GrUhdFlowgraphBackend` intact,
but owns it in a spawned child process and exposes its private backend API over
a bounded pipe.
"""

from __future__ import annotations

import multiprocessing
import time
from threading import Lock

from .backend import GrUhdFlowgraphBackend


class B210BackendUnavailable(ConnectionError):
    """The isolated native backend exited or its control channel was lost."""


class B210BackendTimeout(TimeoutError):
    """A bounded call into the isolated native backend did not complete."""


class B210BackendRemoteError(RuntimeError):
    """A normal Python exception was returned by the isolated backend."""


def _backend_worker(connection, backend_type, constructor_args) -> None:
    """Own one native backend until a successful bounded stop.

    This must remain a module-level function so the ``spawn`` multiprocessing
    context can import it without inheriting any GNU Radio state from the
    adapter process.
    """

    backend = None
    try:
        try:
            backend = backend_type(*constructor_args)
        except BaseException as error:
            connection.send(
                ("INIT", False, type(error).__name__, str(error))
            )
            return
        connection.send(("INIT", True, None))
        while True:
            try:
                request_id, method_name, args, kwargs = connection.recv()
            except EOFError:
                return
            should_exit = False
            try:
                if method_name == "__stop__":
                    value = backend.stop(*args, **kwargs)
                    should_exit = value is not False
                else:
                    value = getattr(backend, method_name)(*args, **kwargs)
                connection.send((request_id, True, value))
            except BaseException as error:
                try:
                    connection.send(
                        (request_id, False, type(error).__name__, str(error))
                    )
                except (BrokenPipeError, EOFError, OSError):
                    return
            if should_exit:
                return
    finally:
        try:
            connection.close()
        except OSError:
            pass


class ProcessIsolatedGrUhdBackend:
    """Parent-side proxy for one child-owned programmatic gr-uhd flowgraph."""

    _STARTUP_TIMEOUT_SECONDS = 120.0
    _OPERATION_TIMEOUT_SECONDS = 5.0
    _REAP_TIMEOUT_SECONDS = 1.0

    def __init__(
        self,
        device_selector: str,
        cpu_format: str,
        otw_format: str,
        *,
        _backend_type=GrUhdFlowgraphBackend,
        _multiprocessing_context=None,
    ) -> None:
        context = _multiprocessing_context or multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = context.Process(
            target=_backend_worker,
            args=(
                child_connection,
                _backend_type,
                (device_selector, cpu_format, otw_format),
            ),
            name="vdeter-b210-gr-uhd",
            daemon=True,
        )
        self._connection = parent_connection
        self._process = process
        self._rpc_lock = Lock()
        self._next_request_id = 1
        self._closed = False
        process.start()
        child_connection.close()
        try:
            if not parent_connection.poll(self._STARTUP_TIMEOUT_SECONDS):
                self._terminate_and_reap()
                raise B210BackendTimeout(
                    "isolated gr-uhd backend did not initialize within 120 seconds"
                )
            try:
                response = parent_connection.recv()
            except (EOFError, OSError) as error:
                raise self._unavailable("initialization", error) from error
            if len(response) < 3 or response[0] != "INIT":
                self._terminate_and_reap()
                raise B210BackendUnavailable(
                    "isolated gr-uhd backend returned an invalid initialization response"
                )
            if not response[1]:
                self._terminate_and_reap()
                raise B210BackendRemoteError(
                    f"{response[2]}: {response[3]}"
                )
        except BaseException:
            self._close_connection()
            raise

    @staticmethod
    def discover(device_selector: str) -> tuple[str, ...]:
        # Discovery does not construct a source, sink, streamer, or flowgraph.
        return GrUhdFlowgraphBackend.discover(device_selector)

    def configure(self, configuration):
        return self._rpc(
            "configure", configuration,
            timeout_seconds=self._STARTUP_TIMEOUT_SECONDS,
        )

    def connect(self) -> None:
        self._rpc("connect")

    def start(self) -> None:
        self._rpc("start")

    def stop(self, timeout_ns: int) -> bool:
        if self._closed:
            return True
        timeout_seconds = max(0.0, timeout_ns / 1_000_000_000)
        if not self._process.is_alive():
            self._reap()
            self._closed = True
            self._close_connection()
            return True
        try:
            result = self._rpc(
                "__stop__",
                timeout_ns,
                timeout_seconds=timeout_seconds,
                terminate_on_timeout=True,
            )
        except B210BackendUnavailable:
            # Native abort already reclaimed the device handles with the child.
            self._reap()
            self._closed = True
            self._close_connection()
            return True
        except B210BackendTimeout:
            # The first call must report the timeout.  The process has been
            # terminated and reaped, so a subsequent adapter stop can retry and
            # finish deterministically.
            return False
        if result is False:
            return False
        self._reap()
        self._closed = True
        self._close_connection()
        return True

    def close(self, timeout_ns: int) -> bool:
        return self.stop(timeout_ns)

    def contain_failure(self) -> None:
        """Terminate a child whose native runtime is no longer trustworthy.

        UHD may leave the Python worker process alive after terminating one of
        its native task-loop threads.  Once the adapter has classified a lost
        hardware-time source as a device disconnect, asking that damaged graph
        to stop normally can consume the entire cleanup deadline.  Process
        isolation makes immediate terminate/reap the deterministic cleanup.
        """

        if self._closed:
            return
        with self._rpc_lock:
            self._terminate_and_reap()
            self._closed = True
            self._close_connection()

    def hardware_time_ns(self) -> int:
        return int(self._rpc("hardware_time_ns"))

    def tx_center_frequency_hz(self) -> int:
        return int(self._rpc("tx_center_frequency_hz"))

    def arm_async_underflow_preflight(
        self, after_samples: int = 8_192, pause_seconds: float = 0.5
    ) -> None:
        self._rpc(
            "arm_async_underflow_preflight", after_samples, pause_seconds
        )

    def submit_tx(
        self,
        data: bytes,
        hardware_time_ns: int,
        center_frequency_hz: int,
        retune_time_ns: int | None,
    ) -> None:
        self._rpc(
            "submit_tx",
            data,
            hardware_time_ns,
            center_frequency_hz,
            retune_time_ns,
        )

    def submit_rx(
        self,
        hardware_time_ns: int,
        sample_count: int,
        center_frequency_hz: int,
    ) -> None:
        self._rpc(
            "submit_rx", hardware_time_ns, sample_count, center_frequency_hz
        )

    def poll_async(self) -> tuple[dict, ...]:
        return tuple(self._rpc("poll_async"))

    def wait_for_activity(self, timeout_seconds: float) -> None:
        # A local bounded wait prevents one child-side wait RPC from blocking a
        # simultaneous adapter stop.  The adapter polls every 10 ms regardless.
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while self._process.is_alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.01))

    def read_rx_cf32(
        self,
        sample_count: int,
        *,
        allow_partial: bool = False,
        expected_hardware_time_ns: int | None = None,
    ):
        return self._rpc(
            "read_rx_cf32",
            sample_count,
            allow_partial=allow_partial,
            expected_hardware_time_ns=expected_hardware_time_ns,
        )

    def _rpc(
        self,
        method_name,
        *args,
        timeout_seconds=None,
        terminate_on_timeout=True,
        **kwargs,
    ):
        timeout = (
            self._OPERATION_TIMEOUT_SECONDS
            if timeout_seconds is None
            else max(0.0, timeout_seconds)
        )
        deadline = time.monotonic() + timeout
        if not self._rpc_lock.acquire(timeout=timeout):
            if terminate_on_timeout:
                self._terminate_and_reap()
            raise B210BackendTimeout(
                f"isolated gr-uhd backend call lock timed out: {method_name}"
            )
        try:
            if self._closed or not self._process.is_alive():
                raise self._unavailable(method_name)
            request_id = self._next_request_id
            self._next_request_id += 1
            try:
                self._connection.send((request_id, method_name, args, kwargs))
            except (BrokenPipeError, EOFError, OSError) as error:
                raise self._unavailable(method_name, error) from error
            remaining = max(0.0, deadline - time.monotonic())
            if not self._connection.poll(remaining):
                if terminate_on_timeout:
                    self._terminate_and_reap()
                raise B210BackendTimeout(
                    f"isolated gr-uhd backend call timed out: {method_name}"
                )
            try:
                response = self._connection.recv()
            except (EOFError, OSError) as error:
                raise self._unavailable(method_name, error) from error
            if len(response) < 3 or response[0] != request_id:
                self._terminate_and_reap()
                raise B210BackendUnavailable(
                    f"isolated gr-uhd backend protocol failure: {method_name}"
                )
            if not response[1]:
                raise B210BackendRemoteError(
                    f"{response[2]}: {response[3]}"
                )
            return response[2]
        finally:
            self._rpc_lock.release()

    def _unavailable(self, operation, cause=None):
        exitcode = self._process.exitcode
        suffix = "" if cause is None else f": {type(cause).__name__}"
        return B210BackendUnavailable(
            "isolated gr-uhd backend became unavailable during "
            f"{operation} (exitcode={exitcode}){suffix}"
        )

    def _terminate_and_reap(self) -> None:
        if self._process.is_alive():
            self._process.terminate()
        self._reap()

    def _reap(self) -> None:
        self._process.join(self._REAP_TIMEOUT_SECONDS)
        if self._process.is_alive():
            self._process.kill()
            self._process.join(self._REAP_TIMEOUT_SECONDS)

    def _close_connection(self) -> None:
        try:
            self._connection.close()
        except OSError:
            pass
