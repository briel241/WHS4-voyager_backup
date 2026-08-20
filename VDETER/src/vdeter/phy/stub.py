"""단계 0 PHY Stub (`stage0_baseline.md` §5.5).

이 구현은 interface 연결을 시험하기 위한 **test scope** stub이며 VDE-TER PHY 처리가
아니다. 다음을 수행하지 않으며 수행했다고 주장하지 않는다.

- FEC, interleaving, rate adaptation, scrambling, bit mapping
- modulation/demodulation, training sequence, burst 생성·검출
- symbol timing, carrier·frequency recovery, Link ID 자동 검출

단계 0 test envelope
- `[packet 길이 4 bytes big-endian][packet bytes][0 padding]`을 test scope sample format
  `test-iq-int16-le`의 sample 경계에 맞춘다.
- 규범 waveform이 아니며 운영 profile에 등록하지 않는다.
- 기준 벡터나 protocol 적합성 증거로 사용하지 않는다.
"""

from __future__ import annotations

from ..common.contracts.phy import (
    BasebandBurst,
    PhyDecodeRequest,
    PhyEncodeRequest,
    PhyRecoveryStatus,
    RecoveredPacket,
    RecoveredPacketSet,
)
from ..common.contracts.radio import (
    SAMPLE_FORMAT_BYTES_PER_SAMPLE,
    SAMPLE_FORMAT_TEST_IQ_INT16_LE,
    SampleBlock,
)
from ..common.errors import Correlation, Details, ErrorCategory, ErrorCode, Layer
from ..common.identifiers import IdSource, LinkId
from ..common.results import Ok, internal_error
from ..common.time import TimeSource
from .models import PhyCapabilities

#: 단계 0 test envelope의 길이 prefix. 규범 burst 형식이 아니다.
STAGE0_TEST_LENGTH_PREFIX_BYTES = 4


class StubPhy:
    """test scope PHY stub. Packet bytes를 test sample block에 담고 되돌린다."""

    __slots__ = ("_time_source", "_id_source", "_capabilities", "_sample_format")

    def __init__(
        self,
        time_source: TimeSource,
        id_source: IdSource,
        capabilities: PhyCapabilities,
        sample_format: str = SAMPLE_FORMAT_TEST_IQ_INT16_LE,
    ) -> None:
        self._time_source = time_source
        self._id_source = id_source
        self._capabilities = capabilities
        self._sample_format = sample_format

    def encode_packet(self, request: PhyEncodeRequest):
        if not isinstance(request, PhyEncodeRequest):
            return internal_error(
                Layer.PHY,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "PhyEncodeRequest가 필요합니다",
            )
        correlation = Correlation(packet_id=request.packet_id)
        if not self._capabilities.supports(request.link_id):
            return internal_error(
                Layer.PHY,
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "endpoint 설정이 허용하지 않는 Link ID입니다",
                correlation=correlation,
                details=Details.of(link_id=request.link_id.value),
            )
        bytes_per_sample = SAMPLE_FORMAT_BYTES_PER_SAMPLE.get(self._sample_format)
        if bytes_per_sample is None:
            return internal_error(
                Layer.PHY,
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "등록되지 않은 sample format입니다",
                correlation=correlation,
                details=Details.of(sample_format=self._sample_format),
            )
        payload = (
            len(request.packet).to_bytes(STAGE0_TEST_LENGTH_PREFIX_BYTES, "big") + request.packet
        )
        remainder = len(payload) % bytes_per_sample
        if remainder:
            payload += bytes(bytes_per_sample - remainder)
        samples = SampleBlock(
            sample_format=self._sample_format,
            sample_rate_hz=self._capabilities.sample_rate_hz,
            sample_count=len(payload) // bytes_per_sample,
            data=payload,
            first_sample_at=request.transmit_at,
        )
        return Ok(
            BasebandBurst(
                burst_id=self._id_source.new_burst_id(),
                packet_id=request.packet_id,
                link_id=request.link_id,
                channel=request.channel,
                transmit_at=request.transmit_at,
                samples=samples,
            )
        )

    def decode_samples(self, request: PhyDecodeRequest):
        if not isinstance(request, PhyDecodeRequest):
            return internal_error(
                Layer.PHY,
                ErrorCode.INVALID_REQUEST,
                ErrorCategory.VALIDATION,
                "PhyDecodeRequest가 필요합니다",
            )
        samples = request.samples
        if samples.sample_format != self._sample_format:
            return internal_error(
                Layer.PHY,
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "이 PHY 구성이 다루지 않는 sample format입니다",
                details=Details.of(
                    expected_sample_format=self._sample_format,
                    sample_format=samples.sample_format,
                ),
            )
        if len(samples.data) < STAGE0_TEST_LENGTH_PREFIX_BYTES:
            return Ok(
                RecoveredPacketSet(
                    channel=request.channel,
                    received_at=samples.first_sample_at,
                    status=PhyRecoveryStatus.NO_BURST_DETECTED,
                    packets=(),
                )
            )
        length = int.from_bytes(samples.data[:STAGE0_TEST_LENGTH_PREFIX_BYTES], "big")
        end = STAGE0_TEST_LENGTH_PREFIX_BYTES + length
        if length == 0 or end > len(samples.data):
            return Ok(
                RecoveredPacketSet(
                    channel=request.channel,
                    received_at=samples.first_sample_at,
                    status=PhyRecoveryStatus.NO_BURST_DETECTED,
                    packets=(),
                )
            )
        link_id = self._detected_link_id(request)
        if link_id is None:
            return internal_error(
                Layer.PHY,
                ErrorCode.UNSUPPORTED_REQUEST,
                ErrorCategory.UNSUPPORTED,
                "허용된 Link ID가 없어 수신 profile을 선택할 수 없습니다",
                details=Details.of(channel=request.channel.name),
            )
        packet = RecoveredPacket(
            packet_id=self._id_source.new_packet_id(),
            link_id=link_id,
            channel=request.channel,
            received_at=samples.first_sample_at,
            packet=samples.data[STAGE0_TEST_LENGTH_PREFIX_BYTES:end],
        )
        return Ok(
            RecoveredPacketSet(
                channel=request.channel,
                received_at=samples.first_sample_at,
                status=PhyRecoveryStatus.RECOVERED,
                packets=(packet,),
            )
        )

    def _detected_link_id(self, request: PhyDecodeRequest) -> LinkId | None:
        """단계 0 stub은 Link ID를 검출하지 않고 허용 집합의 첫 값을 사용한다.

        실제 Link ID 자동 검출은 단계 2·6 PHY 기준선이 구현한다.
        """

        for link_id in request.allowed_link_ids:
            if self._capabilities.supports(link_id):
                return link_id
        if not request.allowed_link_ids and self._capabilities.supported_link_ids:
            return self._capabilities.supported_link_ids[0]
        return None
