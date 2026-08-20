"""M.2092-2 Table 43/44/47/48 logical TBB canonical wire codec."""

from __future__ import annotations

from ...common.contracts.message import (
    BulletinBoardBody,
    ControlStationServiceArea,
    LogicalChannelDefinition,
    LogicalChannelFunction,
    LogicalChannelGroup,
    PhysicalChannelBandwidth,
    PhysicalChannelDefinition,
)
from ...common.errors import ErrorCategory, ErrorCode, Layer
from ...common.exceptions import VdeterValidationError
from ...common.results import InternalResult, Ok, internal_error
from ..messages.codec_base import MessageWireError, invalid_field, reserved, unsupported

_FUNCTION_TO_WIRE = {
    LogicalChannelFunction.BULLETIN_BOARD: 0,
    LogicalChannelFunction.RANDOM_ACCESS: 1,
    LogicalChannelFunction.ANNOUNCEMENT_SIGNALLING: 2,
    LogicalChannelFunction.DATA: 3,
    LogicalChannelFunction.DATA_SIGNALLING: 4,
    LogicalChannelFunction.RANGING: 5,
}
_WIRE_TO_FUNCTION = {value: key for key, value in _FUNCTION_TO_WIRE.items()}
_BANDWIDTH_TO_WIRE = {
    PhysicalChannelBandwidth.KHZ_25: 0,
    PhysicalChannelBandwidth.KHZ_100: 2,
}
_WIRE_TO_BANDWIDTH = {value: key for key, value in _BANDWIDTH_TO_WIRE.items()}


class _BitWriter:
    __slots__ = ("_bits",)

    def __init__(self) -> None:
        self._bits: list[int] = []

    def put(self, value: int, width: int) -> None:
        if not 0 <= value < (1 << width):
            raise invalid_field(f"{width}-bit field 범위를 벗어났습니다")
        self._bits.extend((value >> shift) & 1 for shift in range(width - 1, -1, -1))

    def bytes(self) -> bytes:
        if len(self._bits) % 8:
            raise invalid_field("TBB bit layout이 byte 경계에서 끝나지 않습니다")
        output = bytearray()
        for offset in range(0, len(self._bits), 8):
            value = 0
            for bit in self._bits[offset : offset + 8]:
                value = (value << 1) | bit
            output.append(value)
        return bytes(output)


class _BitReader:
    __slots__ = ("_data", "_position")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    def take(self, width: int) -> int:
        if self._position + width > len(self._data) * 8:
            raise MessageWireError(ErrorCode.LINK_MESSAGE_LENGTH_INVALID, "TBB bit field가 잘렸습니다")
        value = 0
        for _ in range(width):
            byte_index, bit_index = divmod(self._position, 8)
            value = (value << 1) | ((self._data[byte_index] >> (7 - bit_index)) & 1)
            self._position += 1
        return value

    @property
    def remaining_bits(self) -> int:
        return len(self._data) * 8 - self._position


def _signed_to_wire(value: int, width: int) -> int:
    return value & ((1 << width) - 1)


def _signed_from_wire(value: int, width: int) -> int:
    sign = 1 << (width - 1)
    return value - (1 << width) if value & sign else value


def _encode_physical_channels(channels: tuple[PhysicalChannelDefinition, ...]) -> bytes:
    writer = _BitWriter()
    writer.put(len(channels) - 1, 8)
    for channel in channels:
        writer.put(channel.channel_number, 8)
        writer.put(channel.frequency, 12)
        writer.put(0, 1)
        writer.put(_BANDWIDTH_TO_WIRE[channel.bandwidth], 2)
        writer.put(int(channel.transmit_enabled), 1)
        writer.put(channel.random_access_selection_interval, 9)
        writer.put(channel.short_data_message_limit, 7)
        total_definitions = 0
        for group in channel.logical_channel_groups:
            writer.put(len(group.definitions), 6)
            total_definitions += len(group.definitions)
            for definition in group.definitions:
                writer.put(_FUNCTION_TO_WIRE[definition.function], 3)
                writer.put(int(definition.repeat), 1)
                writer.put(definition.duration, 8)
        if total_definitions % 2 == 0:
            writer.put(0, 4)
    return writer.bytes()


def _decode_physical_channels(data: bytes) -> tuple[PhysicalChannelDefinition, ...]:
    reader = _BitReader(data)
    count = reader.take(8) + 1
    channels = []
    for _ in range(count):
        channel_number = reader.take(8)
        frequency = reader.take(12)
        if reader.take(1) != 0:
            raise reserved("Physical Channel reserved bit는 0이어야 합니다")
        bandwidth_value = reader.take(2)
        if bandwidth_value == 1:
            raise unsupported("50 kHz bandwidth는 future-use입니다")
        if bandwidth_value == 3:
            raise invalid_field("정의되지 않은 bandwidth 값 3입니다")
        transmit_enabled = bool(reader.take(1))
        interval = reader.take(9)
        short_limit = reader.take(7)
        groups = []
        total_definitions = 0
        for tdma_index in range(6):
            definition_count = reader.take(6)
            total_definitions += definition_count
            definitions = []
            for _ in range(definition_count):
                function_value = reader.take(3)
                function = _WIRE_TO_FUNCTION.get(function_value)
                if function is None:
                    raise invalid_field("Logical Channel function 6·7은 정의되지 않았습니다")
                definitions.append(
                    LogicalChannelDefinition(
                        function=function,
                        repeat=bool(reader.take(1)),
                        duration=reader.take(8),
                    )
                )
            groups.append(LogicalChannelGroup(tdma_index, tuple(definitions)))
        if total_definitions % 2 == 0 and reader.take(4) != 0:
            raise reserved("Logical Channel parity padding은 0이어야 합니다")
        channels.append(
            PhysicalChannelDefinition(
                channel_number=channel_number,
                frequency=frequency,
                bandwidth=_WIRE_TO_BANDWIDTH[bandwidth_value],
                transmit_enabled=transmit_enabled,
                random_access_selection_interval=interval,
                short_data_message_limit=short_limit,
                logical_channel_groups=tuple(groups),
            )
        )
    if reader.remaining_bits != 0:
        raise MessageWireError(
            ErrorCode.LINK_MESSAGE_LENGTH_INVALID,
            "Physical Channel definition 뒤 trailing bits가 있습니다",
        )
    return tuple(channels)


def _encode_service_area(area: ControlStationServiceArea) -> bytes:
    writer = _BitWriter()
    writer.put(_signed_to_wire(area.longitude_1_tenth_minutes, 18), 18)
    writer.put(_signed_to_wire(area.latitude_1_tenth_minutes, 17), 17)
    writer.put(_signed_to_wire(area.longitude_2_tenth_minutes, 18), 18)
    writer.put(_signed_to_wire(area.latitude_2_tenth_minutes, 17), 17)
    writer.put(area.priority, 2)
    return writer.bytes()


def _decode_service_area(data: bytes) -> ControlStationServiceArea:
    if len(data) != 9:
        raise MessageWireError(ErrorCode.LINK_MESSAGE_LENGTH_INVALID, "Service area는 9 bytes여야 합니다")
    reader = _BitReader(data)
    return ControlStationServiceArea(
        _signed_from_wire(reader.take(18), 18),
        _signed_from_wire(reader.take(17), 17),
        _signed_from_wire(reader.take(18), 18),
        _signed_from_wire(reader.take(17), 17),
        reader.take(2),
    )


def encode_unsigned_bulletin_board(body: BulletinBoardBody) -> InternalResult[bytes]:
    """fingerprint와 signature 앞의 canonical Bulletin Board fields 1..6을 encode한다."""

    if not isinstance(body, BulletinBoardBody):
        return _error(invalid_field("BulletinBoardBody가 필요합니다"))
    if body.tdma_frame_size != 15:
        return _error(unsupported("TDMA frame size 15만 운영 지원합니다"))
    try:
        data = b"".join((
            body.start_time.to_bytes(4, "big"),
            body.validity.to_bytes(2, "big"),
            body.tdma_frame_size.to_bytes(1, "big"),
            _encode_physical_channels(body.physical_channels),
            body.modulation_coding_protocol.to_bytes(1, "big"),
            _encode_service_area(body.service_area),
        ))
        return Ok(data)
    except (MessageWireError, VdeterValidationError) as error:
        return _error(error if isinstance(error, MessageWireError) else invalid_field(str(error)))


def encode_bulletin_board_payload(
    body: BulletinBoardBody, certificate_fingerprint: bytes, authentication_sequence: bytes
) -> InternalResult[bytes]:
    unsigned = encode_unsigned_bulletin_board(body)
    if unsigned.is_err:
        return unsigned
    if not isinstance(certificate_fingerprint, bytes) or len(certificate_fingerprint) != 6:
        return _error(invalid_field("certificate_fingerprint는 6 bytes여야 합니다"))
    if not isinstance(authentication_sequence, bytes) or len(authentication_sequence) != 64:
        return _error(invalid_field("authentication_sequence는 64 bytes여야 합니다"))
    payload = unsigned.value + certificate_fingerprint + authentication_sequence
    if len(payload) > 233:
        return _error(
            MessageWireError(
                ErrorCode.LINK_TRANSFER_SIZE_UNSUPPORTED,
                "logical TBB payload가 233 bytes를 넘습니다",
                ErrorCategory.UNSUPPORTED,
            )
        )
    return Ok(payload)


def decode_bulletin_board_payload(
    data: bytes,
) -> InternalResult[tuple[BulletinBoardBody, bytes, bytes]]:
    if not isinstance(data, bytes) or len(data) < 98 or len(data) > 233:
        return _error(
            MessageWireError(ErrorCode.LINK_TBB_FRAGMENT_SIZE_INVALID, "logical TBB payload 길이가 잘못되었습니다")
        )
    try:
        start_time = int.from_bytes(data[0:4], "big")
        validity = int.from_bytes(data[4:6], "big")
        frame_size = data[6]
        if frame_size in (3, 5):
            raise unsupported("TDMA frame size 3·5는 현재 지원하지 않습니다")
        if frame_size != 15:
            raise invalid_field("정의되지 않은 TDMA frame size입니다")
        physical_data = data[7:-80]
        channels = _decode_physical_channels(physical_data)
        modulation = data[-80]
        if modulation != 0:
            raise reserved("modulation/coding/protocol versions는 0이어야 합니다")
        service_area = _decode_service_area(data[-79:-70])
        fingerprint = data[-70:-64]
        authentication = data[-64:]
        body = BulletinBoardBody(
            start_time=start_time,
            validity=validity,
            tdma_frame_size=frame_size,
            physical_channels=channels,
            modulation_coding_protocol=modulation,
            service_area=service_area,
        )
        return Ok((body, fingerprint, authentication))
    except (MessageWireError, VdeterValidationError) as error:
        return _error(error if isinstance(error, MessageWireError) else invalid_field(str(error)))


def _error(error: MessageWireError):
    return internal_error(
        Layer.LINK,
        error.code,
        error.category,
        error.message,
    )
