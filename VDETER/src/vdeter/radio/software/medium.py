"""Deterministic, bounded, thread-safe Stage 3 Software Radio medium."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from threading import RLock

from ...common.contracts.radio import RadioTxRequest
from ...common.exceptions import InvalidValueError
from ...common.identifiers import PhysicalChannel
from ...common.time import TimePoint


@dataclass(frozen=True, slots=True)
class SoftwareRadioLink:
    source_endpoint_id: str
    target_endpoint_id: str
    channel: PhysicalChannel
    propagation_delay_samples: int = 0

    def __post_init__(self) -> None:
        for name in ("source_endpoint_id", "target_endpoint_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise InvalidValueError(f"SoftwareRadioLink.{name} must be nonempty")
        if not isinstance(self.channel, PhysicalChannel):
            raise InvalidValueError("SoftwareRadioLink.channel is invalid")
        if (
            isinstance(self.propagation_delay_samples, bool)
            or not isinstance(self.propagation_delay_samples, int)
            or self.propagation_delay_samples < 0
        ):
            raise InvalidValueError(
                "SoftwareRadioLink.propagation_delay_samples must be nonnegative"
            )


@dataclass(frozen=True, slots=True)
class _Transmission:
    source_endpoint_id: str
    configure_generation: int
    request: RadioTxRequest


@dataclass(frozen=True, slots=True)
class _RxReservation:
    target_endpoint_id: str
    reservation_key: tuple
    channel: PhysicalChannel
    start_at: TimePoint
    sample_rate_hz: int
    sample_count: int


class SoftwareRadioMedium:
    """Directed medium that sums signals and retains samples for a bounded time."""

    __slots__ = (
        "_links",
        "_transmissions",
        "_rx_reservations",
        "_retention_nanoseconds",
        "_max_retained_transmissions",
        "_lock",
    )

    def __init__(
        self,
        links: tuple[SoftwareRadioLink, ...] = (),
        *,
        retention_nanoseconds: int = 1_000_000_000,
        max_retained_transmissions: int = 4_096,
    ) -> None:
        if not isinstance(links, tuple) or any(
            not isinstance(value, SoftwareRadioLink) for value in links
        ):
            raise InvalidValueError("SoftwareRadioMedium.links is invalid")
        for name, value in (
            ("retention_nanoseconds", retention_nanoseconds),
            ("max_retained_transmissions", max_retained_transmissions),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InvalidValueError(
                    f"SoftwareRadioMedium.{name} must be a positive integer"
                )
        self._links = list(links)
        self._transmissions: dict[tuple[str, int, str], _Transmission] = {}
        self._rx_reservations: dict[tuple[str, tuple], _RxReservation] = {}
        self._retention_nanoseconds = retention_nanoseconds
        self._max_retained_transmissions = max_retained_transmissions
        self._lock = RLock()

    def connect(self, link: SoftwareRadioLink) -> None:
        if not isinstance(link, SoftwareRadioLink):
            raise InvalidValueError("SoftwareRadioMedium.connect requires a link")
        with self._lock:
            self._links.append(link)

    def submit(
        self,
        source_endpoint_id: str,
        request: RadioTxRequest,
        configure_generation: int = 0,
    ) -> None:
        if (
            isinstance(configure_generation, bool)
            or not isinstance(configure_generation, int)
            or configure_generation < 0
        ):
            raise InvalidValueError("configure_generation must be nonnegative")
        key = (source_endpoint_id, configure_generation, request.burst_id.value)
        with self._lock:
            self._purge_locked(request.transmit_at)
            if key in self._transmissions:
                raise InvalidValueError("SoftwareRadioMedium burst_id is duplicated")
            if len(self._transmissions) >= self._max_retained_transmissions:
                self._purge_locked(request.transmit_at, under_pressure=True)
            if len(self._transmissions) >= self._max_retained_transmissions:
                raise InvalidValueError(
                    "SoftwareRadioMedium retained transmission limit reached"
                )
            self._transmissions[key] = _Transmission(
                source_endpoint_id, configure_generation, request
            )

    def cancel(
        self,
        burst_id: str,
        *,
        source_endpoint_id: str | None = None,
        configure_generation: int | None = None,
    ) -> None:
        with self._lock:
            for key in tuple(self._transmissions):
                source, generation, candidate = key
                if candidate != burst_id:
                    continue
                if source_endpoint_id is not None and source != source_endpoint_id:
                    continue
                if configure_generation is not None and generation != configure_generation:
                    continue
                self._transmissions.pop(key, None)

    def register_rx(
        self,
        target_endpoint_id: str,
        reservation_key: tuple,
        channel: PhysicalChannel,
        start_at: TimePoint,
        sample_rate_hz: int,
        sample_count: int,
    ) -> None:
        """Protect signals needed by an accepted, not-yet-completed RX window."""

        key = (target_endpoint_id, reservation_key)
        reservation = _RxReservation(
            target_endpoint_id,
            reservation_key,
            channel,
            start_at,
            sample_rate_hz,
            sample_count,
        )
        with self._lock:
            if key in self._rx_reservations:
                raise InvalidValueError("SoftwareRadioMedium RX reservation is duplicated")
            self._rx_reservations[key] = reservation

    def release_rx(self, target_endpoint_id: str, reservation_key: tuple) -> None:
        key = (target_endpoint_id, reservation_key)
        with self._lock:
            reservation = self._rx_reservations.pop(key, None)
            if reservation is not None:
                self._purge_locked(_reservation_end(reservation))

    def render(
        self,
        target_endpoint_id: str,
        channel: PhysicalChannel,
        start_at: TimePoint,
        sample_rate_hz: int,
        sample_count: int,
    ) -> bytes:
        with self._lock:
            output = [0j] * sample_count
            for link in self._links:
                if link.target_endpoint_id != target_endpoint_id or link.channel != channel:
                    continue
                for transmission in self._transmissions.values():
                    request = transmission.request
                    if (
                        transmission.source_endpoint_id != link.source_endpoint_id
                        or request.channel != channel
                    ):
                        continue
                    if (
                        request.samples.sample_rate_hz != sample_rate_hz
                        or not request.transmit_at.same_clock(start_at)
                    ):
                        continue
                    relative_ns = request.transmit_at.difference_nanoseconds(start_at)
                    offset = _round_half_up(
                        relative_ns * sample_rate_hz, 1_000_000_000
                    )
                    offset += link.propagation_delay_samples
                    values = tuple(
                        complex(i_value, q_value)
                        for i_value, q_value in struct.iter_unpack(
                            "<ff", request.samples.data
                        )
                    )
                    source_begin = max(0, -offset)
                    source_end = min(len(values), sample_count - offset)
                    for source_index in range(
                        source_begin, max(source_begin, source_end)
                    ):
                        output[offset + source_index] += values[source_index]
            rendered = b"".join(
                struct.pack("<ff", value.real, value.imag) for value in output
            )
            self._purge_locked(
                start_at.shifted(_duration_ceil_ns(sample_count, sample_rate_hz))
            )
            return rendered

    def retained_transmission_count(self) -> int:
        """Return a lock-consistent diagnostic count without exposing samples."""

        with self._lock:
            return len(self._transmissions)

    def active_rx_reservation_count(self) -> int:
        with self._lock:
            return len(self._rx_reservations)

    def _purge_locked(
        self, observed_at: TimePoint, *, under_pressure: bool = False
    ) -> None:
        candidates = []
        observed_ns = observed_at.total_nanoseconds()
        for key, transmission in self._transmissions.items():
            request = transmission.request
            if not request.transmit_at.same_clock(observed_at):
                continue
            end_ns = request.transmit_at.total_nanoseconds() + _duration_ceil_ns(
                request.samples.sample_count, request.samples.sample_rate_hz
            )
            expired = end_ns + self._retention_nanoseconds <= observed_ns
            safely_completed = under_pressure and end_ns <= observed_ns
            if (
                (expired or safely_completed)
                and not self._is_protected_locked(transmission)
            ):
                candidates.append((end_ns, key))
        candidates.sort()
        for _end_ns, key in candidates:
            if (
                under_pressure
                and len(self._transmissions) < self._max_retained_transmissions
            ):
                break
            self._transmissions.pop(key, None)

    def _is_protected_locked(self, transmission: _Transmission) -> bool:
        request = transmission.request
        tx_duration_ns = _duration_ceil_ns(
            request.samples.sample_count, request.samples.sample_rate_hz
        )
        for reservation in self._rx_reservations.values():
            if reservation.channel != request.channel:
                continue
            if not request.transmit_at.same_clock(reservation.start_at):
                continue
            for link in self._links:
                if (
                    link.source_endpoint_id != transmission.source_endpoint_id
                    or link.target_endpoint_id != reservation.target_endpoint_id
                    or link.channel != request.channel
                ):
                    continue
                delay_ns = _duration_ceil_ns(
                    link.propagation_delay_samples,
                    request.samples.sample_rate_hz,
                )
                tx_start_ns = request.transmit_at.total_nanoseconds() + delay_ns
                tx_end_ns = tx_start_ns + tx_duration_ns
                rx_start_ns = reservation.start_at.total_nanoseconds()
                rx_end_ns = rx_start_ns + _duration_ceil_ns(
                    reservation.sample_count, reservation.sample_rate_hz
                )
                if tx_start_ns < rx_end_ns and rx_start_ns < tx_end_ns:
                    return True
        return False


def _round_half_up(numerator: int, denominator: int) -> int:
    if numerator >= 0:
        return (numerator + denominator // 2) // denominator
    return -((-numerator + denominator // 2) // denominator)


def _duration_ceil_ns(sample_count: int, sample_rate_hz: int) -> int:
    return (sample_count * 1_000_000_000 + sample_rate_hz - 1) // sample_rate_hz


def _reservation_end(reservation: _RxReservation) -> TimePoint:
    return reservation.start_at.shifted(
        _duration_ceil_ns(reservation.sample_count, reservation.sample_rate_hz)
    )
