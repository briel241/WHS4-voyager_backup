"""Runtime-private conversion between native PHY and Radio device samples.

The public PHY implementation never sees Radio short reads and never changes
Link 11's native rate.  This bridge is the single owner of the deterministic
76.8/307.2 ksps conversion used by the wideband two-channel deployment, and of
the accepted RX-window ledger (`stage6_phy_common_baseline.md` §13.2): Radio
sample events are buffered per accepted window and the native PHY is invoked
exactly once, only on a clean successful window terminal.
"""

from __future__ import annotations

from fractions import Fraction

from ..common.contracts.phy import BasebandBurst, PhyDecodeRequest
from ..common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SampleBlock,
    SampleContinuity,
)
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.results import Ok, internal_error
from ..phy import resampling
from ..phy.burst.rrc import (
    from_cf32le,
    to_cf32le,
    validate_cf32le_full_scale,
)

LINK11_ID = 11
WIDEBAND_LINK_IDS = frozenset((17, 19))
SUPPORTED_LINK_IDS = frozenset((LINK11_ID,)) | WIDEBAND_LINK_IDS

#: §13.2: device-rate window intent로 허용되는 canonical 집합 전체.
CANONICAL_DEVICE_INTENTS = frozenset({(11,), (17,), (19,)})

#: 한 device window의 `cf32le` byte 상한: 8,192 samples * 8 bytes.
DEVICE_WINDOW_BYTES = resampling.DEVICE_SLOT_SAMPLES * 8

#: §13.2 ledger integrity states.
INTEGRITY_CLEAN = "CLEAN"
INTEGRITY_INVALID = "INVALID_AFTER_OVERFLOW_OR_GAP"

#: Radio RX 실패 terminal로 해석하는 오류 code 값.
RX_TERMINAL_ERROR_CODES = frozenset(
    {
        ErrorCode.RADIO_RX_TIMEOUT,
        ErrorCode.RADIO_RX_WINDOW_INCOMPLETE,
        ErrorCode.RADIO_OPERATION_CANCELLED,
        ErrorCode.RADIO_STREAM_FAILED,
        ErrorCode.RADIO_DEVICE_DISCONNECTED,
    }
)


class _WindowEntry:
    """accepted RX window 하나의 ledger value (Runtime-private)."""

    __slots__ = (
        "key",
        "channel",
        "start_ns",
        "duration_ns",
        "slot_function",
        "allowed",
        "native_rate_family",
        "integrity_state",
        "terminal_state",
        "blocks",
        "buffered_bytes",
        "buffered_samples",
        "diagnostic_sample_count",
    )

    def __init__(self, key, channel, start_ns, duration_ns, allowed) -> None:
        self.key = key
        self.channel = channel
        self.start_ns = start_ns
        self.duration_ns = duration_ns
        self.allowed = allowed
        self.slot_function = "CONTROL" if allowed == (LINK11_ID,) else "DATA"
        self.native_rate_family = (
            resampling.NATIVE_SAMPLE_RATE_HZ
            if allowed == (LINK11_ID,)
            else resampling.DEVICE_SAMPLE_RATE_HZ
        )
        self.integrity_state = INTEGRITY_CLEAN
        self.terminal_state = None
        self.blocks = []
        self.buffered_bytes = 0
        self.buffered_samples = 0
        self.diagnostic_sample_count = 0

    def discard_buffer(self) -> None:
        self.blocks = []
        self.buffered_bytes = 0
        self.buffered_samples = 0


def canonicalize_intent(allowed_link_ids) -> tuple:
    """요청 tuple의 중복 제거·오름차순 canonical tuple (§7.9와 동일 규칙)."""

    values = []
    for item in allowed_link_ids:
        values.append(item.value if hasattr(item, "value") else int(item))
    return tuple(sorted(set(values)))


class RuntimePhyRadioBridge:
    """Convert complete device windows and own the accepted RX-window ledger."""

    __slots__ = (
        "_native_phy",
        "_ledger",
        "_generation",
        "_buffer_budget_bytes",
        "_native_reset_count",
        "_native_reset_sink",
    )

    def __init__(self, native_phy, *, buffer_budget_bytes=None) -> None:
        self._native_phy = native_phy
        self._ledger = {}
        self._generation = 1
        self._buffer_budget_bytes = buffer_budget_bytes
        self._native_reset_count = 0
        self._native_reset_sink = None

    def set_native_reset_sink(self, sink) -> None:
        """native PHY reset을 `sink(channel)`에 위임한다 (미설정 시 현행대로).

        ledger는 sink 유무와 무관하게 이 bridge가 비운다.
        """

        self._native_reset_sink = sink

    # --- TX ----------------------------------------------------------------

    def to_device_burst(self, burst: BasebandBurst):
        if not isinstance(burst, BasebandBurst):
            return internal_error(
                Layer.RUNTIME,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "BasebandBurst가 필요합니다",
            )
        correlation = Correlation(packet_id=burst.packet_id, burst_id=burst.burst_id)
        try:
            samples = burst.samples
            if samples.sample_format != SAMPLE_FORMAT_CF32LE:
                raise ValueError("native PHY burst must use cf32le")
            if burst.link_id.value == LINK11_ID:
                if (
                    samples.sample_rate_hz != resampling.NATIVE_SAMPLE_RATE_HZ
                    or samples.sample_count != resampling.NATIVE_SLOT_SAMPLES
                ):
                    raise ValueError("Link 11 native burst rate/count mismatch")
                device_values = resampling.interpolate_slot(from_cf32le(samples.data))
                device_data = to_cf32le(device_values)
            elif burst.link_id.value in WIDEBAND_LINK_IDS:
                if (
                    samples.sample_rate_hz != resampling.DEVICE_SAMPLE_RATE_HZ
                    or samples.sample_count != resampling.DEVICE_SLOT_SAMPLES
                ):
                    raise ValueError("wideband native burst rate/count mismatch")
                device_data = samples.data
            else:
                raise ValueError("unsupported Link ID at the Runtime bridge")
            validate_cf32le_full_scale(device_data)
        except (ArithmeticError, ValueError) as error:
            return internal_error(
                Layer.PHY,
                ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN,
                ErrorCategory.INTERNAL,
                "native PHY→Radio device 변환 불변식이 깨졌습니다",
                correlation=correlation,
                details=Details.of(reason=str(error)),
            )
        device_block = SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE,
            sample_rate_hz=resampling.DEVICE_SAMPLE_RATE_HZ,
            sample_count=resampling.DEVICE_SLOT_SAMPLES,
            data=device_data,
            first_sample_at=samples.first_sample_at,
            continuity=SampleContinuity.CONTIGUOUS,
        )
        return Ok(
            BasebandBurst(
                burst_id=burst.burst_id,
                packet_id=burst.packet_id,
                link_id=burst.link_id,
                channel=burst.channel,
                transmit_at=burst.transmit_at,
                samples=device_block,
            )
        )

    # --- 변환 primitive 호출 (완성된 한 window) ------------------------------

    def to_native_decode_request(self, request: PhyDecodeRequest):
        """Return ``Ok(None)`` when Radio delivered an incomplete/gapped window."""

        if not isinstance(request, PhyDecodeRequest):
            return internal_error(
                Layer.RUNTIME,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "PhyDecodeRequest가 필요합니다",
            )
        samples = request.samples
        canonical = canonicalize_intent(request.allowed_link_ids)
        intent = self._validate_device_intent(canonical)
        if intent is not None:
            return intent
        if samples.sample_format != SAMPLE_FORMAT_CF32LE:
            return internal_error(
                Layer.RUNTIME,
                ErrorCode.RADIO_UNSUPPORTED_SETTING,
                ErrorCategory.VALIDATION,
                "Runtime PHY bridge는 cf32le Radio samples만 지원합니다",
                details=Details.of(sample_format=samples.sample_format),
            )
        if samples.sample_rate_hz != resampling.DEVICE_SAMPLE_RATE_HZ:
            return self._configuration_error(
                "Radio RX window가 device sample rate와 일치하지 않습니다",
                canonical,
                actual_sample_rate_hz=samples.sample_rate_hz,
            )
        if (
            samples.sample_count != resampling.DEVICE_SLOT_SAMPLES
            or samples.continuity is not SampleContinuity.CONTIGUOUS
        ):
            self.reset_stream(request.channel)
            return Ok(None)
        if canonical != (LINK11_ID,):
            return Ok(request)
        try:
            native_values = resampling.decimate_slot(from_cf32le(samples.data))
            native_data = to_cf32le(native_values)
        except (ArithmeticError, ValueError) as error:
            self.reset_stream(request.channel)
            return internal_error(
                Layer.PHY,
                ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN,
                ErrorCategory.INTERNAL,
                "Radio device→Link 11 native 변환 불변식이 깨졌습니다",
                details=Details.of(reason=str(error)),
            )
        return Ok(
            PhyDecodeRequest(
                channel=request.channel,
                samples=SampleBlock(
                    sample_format=SAMPLE_FORMAT_CF32LE,
                    sample_rate_hz=resampling.NATIVE_SAMPLE_RATE_HZ,
                    sample_count=resampling.NATIVE_SLOT_SAMPLES,
                    data=native_data,
                    first_sample_at=samples.first_sample_at,
                    continuity=SampleContinuity.CONTIGUOUS,
                ),
                allowed_link_ids=request.allowed_link_ids,
            )
        )

    # --- §13.2 accepted RX-window ledger ------------------------------------

    def begin_generation(self, generation: int) -> None:
        """Radio configure가 적용될 때 새 generation으로 ledger를 비운다."""

        self._generation = int(generation)
        self._ledger.clear()

    def set_buffer_budget(self, budget_bytes) -> None:
        self._buffer_budget_bytes = budget_bytes

    def validate_window_intent(self, allowed_link_ids):
        """device-rate window intent를 canonical tuple로 검증한다."""

        canonical = canonicalize_intent(allowed_link_ids)
        error = self._validate_device_intent(canonical)
        if error is not None:
            return error
        return Ok(canonical)

    def validate_window_timing(self, duration_nanoseconds: int, sample_rate_hz: int):
        """§13.2 item 4: MAC plan 시간과 expected count의 정합만 검증한다."""

        expected = (
            duration_nanoseconds * sample_rate_hz + 500_000_000
        ) // 1_000_000_000
        if (
            sample_rate_hz != resampling.DEVICE_SAMPLE_RATE_HZ
            or expected != resampling.DEVICE_SLOT_SAMPLES
        ):
            return internal_error(
                Layer.RUNTIME,
                ErrorCode.CONFIG_SEMANTIC_INVALID,
                ErrorCategory.VALIDATION,
                "MAC RX window plan 시간이 device slot expected count와 일치하지 않습니다",
                details=Details.of(
                    duration_nanoseconds=duration_nanoseconds,
                    sample_rate_hz=sample_rate_hz,
                    expected_sample_count=expected,
                ),
            )
        return Ok(expected)

    def admit_window(self, channel, start_at, duration_nanoseconds, canonical_allowed):
        """Radio RX admission 성공 뒤에만 ledger entry를 만든다."""

        key = (
            self._generation,
            channel.name,
            start_at.total_nanoseconds(),
            duration_nanoseconds,
        )
        if key in self._ledger:
            return self._invariant_error(
                "이미 admitted RX window key로 중복 ledger entry를 만들 수 없습니다",
                key=repr(key),
            )
        self._ledger[key] = _WindowEntry(
            key,
            channel,
            start_at.total_nanoseconds(),
            duration_nanoseconds,
            tuple(canonical_allowed),
        )
        return Ok(None)

    def observe_samples(self, received):
        """sample event를 유일한 accepted interval에 결속해 terminal까지 보관한다."""

        block = received.samples
        epoch_ns = block.first_sample_at.total_nanoseconds()
        channel_name = received.channel.name
        matches = [
            entry
            for entry in self._ledger.values()
            if entry.channel.name == channel_name
            and entry.start_ns <= epoch_ns < entry.start_ns + entry.duration_ns
        ]
        if len(matches) != 1:
            return self._invariant_error(
                "sample event가 정확히 하나의 accepted RX window에 결속되지 않습니다",
                channel=channel_name,
                epoch_nanoseconds=epoch_ns,
                match_count=len(matches),
            )
        entry = matches[0]
        if entry.integrity_state == INTEGRITY_INVALID:
            # 진단 count만 수행하고 sample bytes는 보관하지 않는다.
            entry.diagnostic_sample_count += block.sample_count
            return Ok(None)
        if block.continuity is not SampleContinuity.CONTIGUOUS:
            self._invalidate(entry)
            entry.diagnostic_sample_count += block.sample_count
            return Ok(None)
        expected_epoch = self._expected_epoch(entry)
        if epoch_ns != expected_epoch:
            # enum과 무관한 implicit discontinuity.
            self._invalidate(entry)
            entry.diagnostic_sample_count += block.sample_count
            return Ok(None)
        if block.sample_format != SAMPLE_FORMAT_CF32LE:
            return self._invariant_error(
                "accepted RX window sample format이 cf32le가 아닙니다",
                sample_format=block.sample_format,
            )
        appended = entry.buffered_bytes + len(block.data)
        if appended > DEVICE_WINDOW_BYTES:
            self._close_invariant(entry)
            return self._invariant_error(
                "window buffer 상한(65,536 bytes)을 넘는 sample 할당을 시도했습니다",
                buffered_bytes=entry.buffered_bytes,
                incoming_bytes=len(block.data),
            )
        budget = self._buffer_budget_bytes
        if budget is not None:
            total = sum(item.buffered_bytes for item in self._ledger.values())
            if total + len(block.data) > budget:
                self._close_invariant(entry)
                return self._invariant_error(
                    "bridge 구성 buffer 상한을 넘는 sample 할당을 시도했습니다",
                    total_buffered_bytes=total,
                    budget_bytes=budget,
                )
        entry.blocks.append(block)
        entry.buffered_bytes = appended
        entry.buffered_samples += block.sample_count
        return Ok(None)

    def record_rx_fault(self, error_code, details: dict):
        """Radio LAYER_ERROR event를 ledger에 반영한다 (nonterminal overflow 포함)."""

        entry = self._entry_from_details(details)
        if entry is None:
            # accepted ledger 밖 fault는 진단 기록 대상일 뿐 상태를 만들지 않는다.
            return Ok(None)
        if error_code == ErrorCode.RADIO_RX_OVERFLOW:
            if entry.integrity_state == INTEGRITY_CLEAN:
                self._invalidate(entry)
            return Ok(None)
        if error_code in RX_TERMINAL_ERROR_CODES:
            entry.terminal_state = error_code
            if entry.integrity_state == INTEGRITY_CLEAN:
                entry.discard_buffer()
                self._reset_native(entry.channel)
            else:
                entry.discard_buffer()
            del self._ledger[entry.key]
            return Ok(None)
        return Ok(None)

    def complete_window(self, completion):
        """성공 terminal: CLEAN·완전한 한 연속 block일 때만 native 요청을 만든다.

        `complete_device_window` + `to_native_decode_request`의 합성이며
        반환 의미는 분리 전과 같다.
        """

        prepared = self.complete_device_window(completion)
        if prepared.is_err or prepared.value is None:
            return prepared
        return self.to_native_decode_request(prepared.value)

    def complete_device_window(self, completion):
        """성공 terminal을 device-rate PhyDecodeRequest까지만 만든다 (리샘플 제외).

        이 bridge가 소유한 상태를 건드리는 구간은 여기까지다.
        """

        key = (
            completion.configure_generation,
            completion.channel.name,
            completion.requested_start_at.total_nanoseconds(),
            completion.requested_duration_nanoseconds,
        )
        entry = self._ledger.get(key)
        if entry is None:
            return self._invariant_error(
                "성공 terminal이 accepted ledger entry와 결속되지 않습니다",
                key=repr(key),
            )
        del self._ledger[key]
        if entry.integrity_state != INTEGRITY_CLEAN:
            entry.terminal_state = "COMPLETED_ON_INVALID"
            entry.discard_buffer()
            return self._invariant_error(
                "CLEAN이 아닌 ledger에 성공 terminal이 도착했습니다 (Stage 3 계약 위반)",
                integrity_state=entry.integrity_state,
            )
        entry.terminal_state = "COMPLETED"
        if (
            len(entry.blocks) == 0
            or entry.buffered_samples != resampling.DEVICE_SLOT_SAMPLES
            or entry.blocks[0].first_sample_at.total_nanoseconds() != entry.start_ns
        ):
            entry.discard_buffer()
            self._reset_native(entry.channel)
            return self._invariant_error(
                "성공 terminal의 buffered sample이 정확히 한 연속 8,192-sample block이 아닙니다",
                buffered_samples=entry.buffered_samples,
            )
        first = entry.blocks[0]
        data = b"".join(block.data for block in entry.blocks)
        window_block = SampleBlock(
            sample_format=SAMPLE_FORMAT_CF32LE,
            sample_rate_hz=resampling.DEVICE_SAMPLE_RATE_HZ,
            sample_count=resampling.DEVICE_SLOT_SAMPLES,
            data=data,
            first_sample_at=first.first_sample_at,
            continuity=SampleContinuity.CONTIGUOUS,
        )
        entry.discard_buffer()
        from ..common.identifiers import LinkId

        request = PhyDecodeRequest(
            channel=entry.channel,
            samples=window_block,
            allowed_link_ids=tuple(LinkId(value) for value in entry.allowed),
        )
        return Ok(request)

    def ledger_snapshot(self) -> tuple:
        """진단·시험용 ledger 상태 (공개 DTO가 아니다)."""

        return tuple(
            {
                "key": entry.key,
                "slot_function": entry.slot_function,
                "allowed_link_ids": entry.allowed,
                "native_rate_family": entry.native_rate_family,
                "integrity_state": entry.integrity_state,
                "terminal_state": entry.terminal_state,
                "buffered_bytes": entry.buffered_bytes,
                "diagnostic_sample_count": entry.diagnostic_sample_count,
            }
            for entry in self._ledger.values()
        )

    @property
    def native_reset_count(self) -> int:
        return self._native_reset_count

    # --- 내부 ----------------------------------------------------------------

    def _expected_epoch(self, entry: _WindowEntry) -> int:
        if not entry.blocks:
            return entry.start_ns
        exact = Fraction(entry.start_ns) + Fraction(
            entry.buffered_samples * 1_000_000_000, resampling.DEVICE_SAMPLE_RATE_HZ
        )
        floor = exact.numerator // exact.denominator
        remainder = exact - floor
        return int(floor + (1 if remainder * 2 >= 1 else 0))

    def _invalidate(self, entry: _WindowEntry) -> None:
        entry.integrity_state = INTEGRITY_INVALID
        entry.discard_buffer()
        self._reset_native(entry.channel)

    def _close_invariant(self, entry: _WindowEntry) -> None:
        entry.terminal_state = "INTERNAL_INVARIANT"
        entry.discard_buffer()
        self._ledger.pop(entry.key, None)

    def _entry_from_details(self, details: dict):
        channel = details.get("channel")
        seconds = details.get("requested_start_seconds")
        nanoseconds = details.get("requested_start_nanoseconds")
        duration = details.get("requested_duration_nanoseconds")
        generation = details.get("configure_generation")
        if None in (channel, seconds, nanoseconds, duration, generation):
            return None
        key = (
            generation,
            channel,
            seconds * 1_000_000_000 + nanoseconds,
            duration,
        )
        return self._ledger.get(key)

    def _reset_native(self, channel) -> None:
        self._native_reset_count += 1
        self.reset_stream(channel)

    def reset_stream(self, channel=None) -> None:
        sink = self._native_reset_sink
        if sink is not None:
            sink(channel)
        else:
            reset = getattr(self._native_phy, "reset_stream", None)
            if reset is not None:
                reset(channel)
        if channel is None:
            self._ledger.clear()

    def _validate_device_intent(self, canonical: tuple):
        if canonical not in CANONICAL_DEVICE_INTENTS:
            if not canonical:
                message = "device-rate RX window에는 비어 있지 않은 allowed_link_ids가 필요합니다"
            elif not set(canonical) <= SUPPORTED_LINK_IDS:
                message = "device-rate RX window에 지원되지 않는 Link ID가 있습니다"
            elif set(canonical) & {LINK11_ID} and set(canonical) & WIDEBAND_LINK_IDS:
                message = "한 device-rate RX window에 두 native rate family를 섞을 수 없습니다"
            else:
                message = (
                    "device-rate RX window intent는 정확히 (11,), (17,), (19,) 중 하나여야 합니다"
                )
            return self._configuration_error(message, canonical)
        return None

    def _invariant_error(self, message: str, **details):
        return internal_error(
            Layer.RUNTIME,
            ErrorCode.PHY_INTERNAL_INVARIANT_BROKEN,
            ErrorCategory.INTERNAL,
            message,
            details=Details.of(**details),
        )

    @staticmethod
    def _configuration_error(message: str, allowed, **details):
        return internal_error(
            Layer.RUNTIME,
            ErrorCode.CONFIG_SEMANTIC_INVALID,
            ErrorCategory.VALIDATION,
            message,
            details=Details.of(
                allowed_link_ids=tuple(sorted(allowed)),
                **details,
            ),
        )


__all__ = (
    "CANONICAL_DEVICE_INTENTS",
    "DEVICE_WINDOW_BYTES",
    "INTEGRITY_CLEAN",
    "INTEGRITY_INVALID",
    "RX_TERMINAL_ERROR_CODES",
    "RuntimePhyRadioBridge",
    "canonicalize_intent",
)
