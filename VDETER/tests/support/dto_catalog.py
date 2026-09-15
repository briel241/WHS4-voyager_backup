"""단계 0 공개 DTO catalogue와 표본 instance.

`TC-ARCH-005`(immutability)와 `TC-ARCH-006`(reference JSON round-trip)이 모든 공개 DTO를
빠짐없이 검사하기 위해 사용한다. 새 공개 DTO를 추가하면 이 catalogue에도 표본을 넣어야
한다. 표본 값은 시험용이며 규범 값이 아니다.
"""

from __future__ import annotations

from vdeter.common.addressing import (
    Addressed,
    Broadcast,
    NodeDestination,
    NodeSource,
    OriginalDestination,
    OriginalSource,
    RouteAddresses,
)
from vdeter.common.contracts.link import (
    BuiltPacket,
    DataFragmentRef,
    EncodedPacket,
    FragmentProcessResult,
    FragmentProcessStatus,
    LinkAnalyzeRequest,
    LinkBuildRequest,
    LinkDecodeRequest,
    LinkFragmentProcessRequest,
    LinkPacketEncodeRequest,
    LinkReassemblyControlRequest,
    LinkTransferControlRequest,
    LinkTransferControlResult,
    LinkTransferProfileRequirement,
    ReassemblyControlKind,
    ReassemblyControlResult,
    TbbAssemblyControlKind,
    TbbAssemblyControlRequest,
    TbbAssemblyControlResult,
    TbbEncodeRequest,
    TbbEncodedPacketSet,
    TransferControlKind,
    TransferKind,
    TransferPacketOutcome,
    TransferPacketProgress,
    TransferProgressSnapshot,
    TransferRequirement,
)
from vdeter.common.contracts.mac import (
    MacAction,
    MacActionKind,
    MacActionSet,
    MacMessageInput,
    PacketDecodeAction,
    RequirementAccepted,
    SessionControlKind,
    SessionControlRequest,
    SessionControlResult,
    TransmissionPlan,
    TransmissionProcedure,
)
from vdeter.common.contracts.message import (
    AcknowledgeNackBody,
    BulletinBoardBody,
    ControlStationServiceArea,
    DataContinuationFragmentBody,
    DataEndFragmentBody,
    DataStartFragmentBody,
    DecodedMessage,
    DecodedMessages,
    FragmentRef,
    LogicalChannelDefinition,
    LogicalChannelFunction,
    LogicalChannelGroup,
    MediaAccessControlBody,
    MessageEncodeRequest,
    PaddingBody,
    PhysicalChannelBandwidth,
    PhysicalChannelDefinition,
    ResourceAllocationBody,
    ResourceRequestBody,
    ShortDataAckBody,
    ShortDataNoAckBody,
    TbbAssemblyStatus,
    TbbAssemblyUpdate,
    TbbCandidateKey,
    TbbContinuationFragmentBody,
    TbbEndFragmentBody,
    TbbStartFragmentBody,
    VerifiedTbb,
)
from vdeter.common.contracts.phy import (
    BasebandBurst,
    PhyDecodeRequest,
    PhyEncodeRequest,
    PhyRecoveryStatus,
    PhyTxCompleted,
    RecoveredPacket,
    RecoveredPacketSet,
)
from vdeter.common.contracts.presentation import (
    ApplicationData,
    PresentationData,
    PresentationDecodeRequest,
    PresentationEncodeRequest,
)
from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    SAMPLE_FORMAT_TEST_IQ_INT16_LE,
    ClockReference,
    RadioChannelSetting,
    RadioChannelBinding,
    RadioB210StreamConfiguration,
    RadioTimingConfiguration,
    RadioTimingProfile,
    RadioResourceLimits,
    RadioCapabilities,
    RadioAppliedChannelSetting,
    RadioAppliedSettings,
    RadioTimingStatus,
    RadioMappingState,
    RadioClockState,
    RadioPpsState,
    RadioUtcFlag,
    RadioQueueStatus,
    RadioEventDeliveryStatus,
    RadioRxWindowCompleted,
    RadioConfiguration,
    RadioConfigurationApplied,
    RadioConfigureRequest,
    RadioDeviceState,
    RadioRxAccepted,
    RadioRxRequest,
    RadioSamplesReceived,
    RadioStarted,
    RadioStartRequest,
    RadioStatusRequest,
    RadioStatusSnapshot,
    RadioStopped,
    RadioStopRequest,
    RadioTxAccepted,
    RadioTxCompleted,
    RadioTxRequest,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.contracts.service import (
    CancelAccepted,
    CancelTransferRequest,
    EndpointHealth,
    ReceivedApplicationData,
    ServiceRole,
    ServiceStartRequest,
    ServiceStarted,
    ServiceStopped,
    ServiceStopRequest,
    SubmitTransferRequest,
    TransferAccepted,
    TransferState,
    TransferStatusRequest,
    TransferStatusSnapshot,
)
from vdeter.common.errors import (
    Correlation,
    Details,
    ErrorCategory,
    ErrorCode,
    InternalProtocolError,
    Layer,
    ServiceError,
    ServiceErrorCategory,
)
from vdeter.common.events import (
    EventPublished,
    EventSeverity,
    InternalEvent,
    InternalEventType,
    ServiceEvent,
    ServiceEventType,
)
from vdeter.common.identifiers import (
    DeterministicIdSource,
    LinkId,
    PayloadFormat,
    PhysicalChannel,
    SequentialId,
    StationId,
    Vpfi,
)
from vdeter.common.time import ClockDomain, ClockMapping, TimePoint

_IDS = DeterministicIdSource(20260819)

TRANSFER_ID = _IDS.new_transfer_id()
SESSION_HANDLE = _IDS.new_session_handle()
PACKET_ID = _IDS.new_packet_id()
BURST_ID = _IDS.new_burst_id()

UTC = TimePoint(1_700_000_000, 123, ClockDomain.UTC, "test-utc")
MONOTONIC = TimePoint(42, 0, ClockDomain.MONOTONIC, "test-monotonic")
HARDWARE = TimePoint(7, 0, ClockDomain.RADIO_HARDWARE, "test-radio")

CHANNEL = PhysicalChannel("stage0-test-tx")
LINK_ID = LinkId(63)
STATION = StationId(111111111)
PEER = StationId(222222222)
PAYLOAD_FORMAT = PayloadFormat("test.stage0.opaque")
VPFI = Vpfi(268435455)
SEQUENTIAL_ID = SequentialId(16383)

ROUTE = RouteAddresses(
    original_source=OriginalSource(STATION),
    original_destination=OriginalDestination(Addressed(PEER)),
    node_source=NodeSource(STATION),
    node_destination=NodeDestination(Broadcast()),
)

DETAILS = Details.of(reason="stage0", payload_size=12, flags=(1, 2))
CORRELATION = Correlation(
    transfer_id=TRANSFER_ID,
    session_handle=SESSION_HANDLE,
    sequential_id=SEQUENTIAL_ID,
    packet_id=PACKET_ID,
    fragment_index=0,
    fragment_count=2,
    burst_id=BURST_ID,
)

SAMPLES = SampleBlock(
    sample_format=SAMPLE_FORMAT_TEST_IQ_INT16_LE,
    sample_rate_hz=4000,
    sample_count=2,
    data=bytes(8),
    first_sample_at=UTC,
    continuity=SampleContinuity.CONTIGUOUS,
)

CHANNEL_SETTING = RadioChannelSetting(
    channel=CHANNEL,
    center_frequency_hz=100_000_000,
    sample_rate_hz=4000,
    tx_gain_db=0,
    rx_gain_db=0,
)

RADIO_CONFIGURATION = RadioConfiguration(
    adapter_kind="FAKE",
    device_selector="",
    clock_reference=ClockReference.INTERNAL,
    channels=(CHANNEL_SETTING,),
    sample_format=SAMPLE_FORMAT_TEST_IQ_INT16_LE,
)

INTERNAL_ERROR = InternalProtocolError(
    layer=Layer.LINK,
    code=ErrorCode.INVALID_REQUEST,
    category=ErrorCategory.VALIDATION,
    message="stage0 표본 오류",
    retryable=False,
    correlation=CORRELATION,
    details=DETAILS,
)

SERVICE_ERROR = ServiceError(
    code="INVALID_REQUEST",
    category=ServiceErrorCategory.VALIDATION,
    message="stage0 표본 공개 오류",
    retryable=False,
    transfer_id=TRANSFER_ID,
    details=Details.of(reason="stage0"),
)

PRESENTATION_DATA = PresentationData(
    transfer_id=TRANSFER_ID,
    route=ROUTE,
    vpfi=VPFI,
    data=b"\x0f\xff\xff\xffpayload",
    message_format=None,
    ack_required=False,
)

DECODED_MESSAGE = DecodedMessage(
    message_type=93,
    length=19,
    route=ROUTE,
    session_handle_candidate=SESSION_HANDLE,
    sequential_id=SEQUENTIAL_ID,
    fragment=FragmentRef(index=0, count=2),
    body=ShortDataNoAckBody(
        source_id=STATION,
        session_id=0,
        destination=Broadcast(),
        segment=b"payload",
    ),
    presentation_data=b"payload",
)

RADIO_TIMING = RadioTimingConfiguration(
    profile=RadioTimingProfile.INTERNAL_FREE_RUNNING,
    clock_source=ClockReference.INTERNAL,
    time_source=ClockReference.INTERNAL,
    pps_source=ClockReference.NONE,
    mapping_attempt_count=8,
    mapping_valid_for_nanoseconds=1_000_000_000,
    max_mapping_uncertainty_ns=10_000_000,
)
RADIO_LIMITS = RadioResourceLimits(64, 8_388_608, 64, 64, 8_388_608, 5_000_000_000)
RADIO_BINDING = RadioChannelBinding(CHANNEL, 25_000, 25_000, True, True)
RADIO_APPLIED_CHANNEL = RadioAppliedChannelSetting(
    CHANNEL, 100_000_000, 100_000_000, 4000, 4000, 4000, 0, 0, 0, 0,
    25_000, 25_000, 25_000, RADIO_BINDING,
)
RADIO_APPLIED_SETTINGS = RadioAppliedSettings(
    "software:stage3", 1, (RADIO_APPLIED_CHANNEL,), None, None, None, None
)
RADIO_MAPPING = ClockMapping(
    source_domain=ClockDomain.UTC,
    source_clock_id=UTC.clock_id,
    target_domain=ClockDomain.RADIO_HARDWARE,
    target_clock_id=HARDWARE.clock_id,
    source_reference=UTC,
    target_reference=HARDWARE,
    uncertainty_nanoseconds=10_000_000,
)
RADIO_TIMING_STATUS = RadioTimingStatus(
    RadioTimingProfile.INTERNAL_FREE_RUNNING,
    RadioClockState.FREE_RUNNING,
    RadioPpsState.PPS_NOT_PRESENT,
    (RadioUtcFlag.HOST_ESTIMATE, RadioUtcFlag.NOT_UTC_DISCIPLINED, RadioUtcFlag.UTC_ACCURACY_UNVERIFIED),
    RadioMappingState.VALID,
    RADIO_MAPPING,
    MONOTONIC,
    MONOTONIC.shifted(1_000_000_000),
)

DECODED_MESSAGES = DecodedMessages(
    packet_id=PACKET_ID, link_id=LINK_ID, received_at=UTC, messages=(DECODED_MESSAGE,)
)

RECOVERED_PACKET = RecoveredPacket(
    packet_id=PACKET_ID, link_id=LINK_ID, channel=CHANNEL, received_at=UTC, packet=b"packet"
)

RECOVERED_SET = RecoveredPacketSet(
    channel=CHANNEL,
    received_at=UTC,
    status=PhyRecoveryStatus.RECOVERED,
    packets=(RECOVERED_PACKET,),
)

TRANSMISSION_PLAN = TransmissionPlan(
    transfer_id=TRANSFER_ID,
    link_id=LINK_ID,
    channel=CHANNEL,
    transmit_at=UTC,
    procedure=TransmissionProcedure.SHORT_DATA,
    route=ROUTE,
    session_handle=SESSION_HANDLE,
)

BUILT_PACKET = BuiltPacket(
    packet_id=PACKET_ID,
    transfer_id=TRANSFER_ID,
    link_id=LINK_ID,
    packet=b"packet",
    session_handle=SESSION_HANDLE,
    sequential_id=SEQUENTIAL_ID,
    fragment=FragmentRef(index=1, count=2),
)

BASEBAND_BURST = BasebandBurst(
    burst_id=BURST_ID,
    packet_id=PACKET_ID,
    link_id=LINK_ID,
    channel=CHANNEL,
    transmit_at=UTC,
    samples=SAMPLES,
)

RECEIVED_APPLICATION_DATA = ReceivedApplicationData(
    original_source=PEER,
    original_destination=Addressed(STATION),
    payload_format=PAYLOAD_FORMAT,
    payload=b"payload",
    received_at=UTC,
)


def _stage1_dto_samples() -> tuple:
    groups = tuple(LogicalChannelGroup(index, ()) for index in range(6))
    physical_channel = PhysicalChannelDefinition(
        channel_number=1,
        frequency=2,
        bandwidth=PhysicalChannelBandwidth.KHZ_25,
        transmit_enabled=True,
        random_access_selection_interval=3,
        short_data_message_limit=4,
        logical_channel_groups=groups,
    )
    service_area = ControlStationServiceArea(0, 0, 0, 0, 0)
    bulletin_board = BulletinBoardBody(
        start_time=1,
        validity=5,
        tdma_frame_size=15,
        physical_channels=(physical_channel,),
        modulation_coding_protocol=0,
        service_area=service_area,
    )
    tbb_key = TbbCandidateKey(STATION, 1, 2)
    encoded_packet = EncodedPacket(
        packet_id=PACKET_ID,
        link_id=LinkId(11),
        packet=b"messagecrc!",
        message_types=(0,),
        padding_size=1,
        crc=b"crc!",
    )
    data_body = DataStartFragmentBody(STATION, 0, Addressed(PEER), 2, 0, 1, b"d")
    data_message = DecodedMessage(message_type=74, length=16, body=data_body)
    message_bodies = (
        ("MediaAccessControlBody", MediaAccessControlBody(STATION, 0, 1, 2, 0)),
        ("ResourceAllocationBody", ResourceAllocationBody(STATION, Broadcast(), 1, 2, LinkId(11), 1, 0, 3)),
        ("AcknowledgeNackBody", AcknowledgeNackBody(STATION, Addressed(PEER), 0, (1, 2, 3), 4, 1, 0)),
        ("TbbStartFragmentBody", TbbStartFragmentBody(STATION, 1, 2, 3, 2, b"a")),
        ("TbbContinuationFragmentBody", TbbContinuationFragmentBody(STATION, 1, 3, 1, b"b")),
        ("TbbEndFragmentBody", TbbEndFragmentBody(STATION, 1, 3, 1, b"c")),
        ("DataStartFragmentBody", data_body),
        ("DataContinuationFragmentBody", DataContinuationFragmentBody(STATION, 0, Addressed(PEER), 3, 2, 1, b"e")),
        ("DataEndFragmentBody", DataEndFragmentBody(STATION, 0, Addressed(PEER), 2, 2, 0, b"f")),
        ("PaddingBody", PaddingBody()),
        ("ResourceRequestBody", ResourceRequestBody(STATION, PEER, Broadcast(), Addressed(PEER), 0, 2)),
        ("ShortDataAckBody", ShortDataAckBody(STATION, 0, Addressed(PEER), 0, b"s")),
        ("ShortDataNoAckBody.stage1", ShortDataNoAckBody(STATION, 0, Broadcast(), b"s")),
    )
    return (
        *message_bodies,
        ("LogicalChannelDefinition", LogicalChannelDefinition(LogicalChannelFunction.DATA, False, 1)),
        ("LogicalChannelGroup", groups[0]),
        ("PhysicalChannelDefinition", physical_channel),
        ("ControlStationServiceArea", service_area),
        ("BulletinBoardBody", bulletin_board),
        ("TbbCandidateKey", tbb_key),
        (
            "VerifiedTbb",
            VerifiedTbb(STATION, 1, 2, 3, bulletin_board, bytes(6), bytes(64), 1, (PACKET_ID,), UTC),
        ),
        (
            "TbbAssemblyUpdate",
            TbbAssemblyUpdate(tbb_key, TbbAssemblyStatus.STARTED, 1, 2, (PACKET_ID,), UTC),
        ),
        ("MessageEncodeRequest", MessageEncodeRequest(message_bodies[0][1], CORRELATION)),
        (
            "DecodedMessages.stage1",
            DecodedMessages(
                PACKET_ID,
                LinkId(11),
                UTC,
                messages=(),
                verified_tbbs=(VerifiedTbb(STATION, 1, 2, 3, bulletin_board, bytes(6), bytes(64), 1, (PACKET_ID,), UTC),),
                tbb_assembly_updates=(TbbAssemblyUpdate(tbb_key, TbbAssemblyStatus.VERIFIED, 1, 1, (PACKET_ID,), UTC),),
            ),
        ),
        ("LinkTransferProfileRequirement", LinkTransferProfileRequirement(LinkId(11), True, 1, 1)),
        ("LinkPacketEncodeRequest", LinkPacketEncodeRequest(LinkId(11), (MessageEncodeRequest(message_bodies[0][1], CORRELATION),), CORRELATION)),
        ("EncodedPacket", encoded_packet),
        ("TbbEncodeRequest", TbbEncodeRequest(STATION, 1, 2, 3, bulletin_board, CORRELATION)),
        ("TbbEncodedPacketSet", TbbEncodedPacketSet((encoded_packet,), bytes(6), bytes(64))),
        ("DataFragmentRef", DataFragmentRef(0, 1, 2, 0)),
        ("LinkFragmentProcessRequest", LinkFragmentProcessRequest(PACKET_ID, LinkId(11), UTC, SESSION_HANDLE, 0, ROUTE, data_message)),
        (
            "FragmentProcessResult",
            FragmentProcessResult(FragmentProcessStatus.INCOMPLETE, SESSION_HANDLE, DataFragmentRef(0, 1, 2, 0), ROUTE, None, None, (PACKET_ID,)),
        ),
        ("LinkReassemblyControlRequest", LinkReassemblyControlRequest(ReassemblyControlKind.OPEN, SESSION_HANDLE, ROUTE, CORRELATION)),
        ("ReassemblyControlResult", ReassemblyControlResult(ReassemblyControlKind.OPEN, SESSION_HANDLE, True, 0, 0)),
        ("TransferPacketProgress", TransferPacketProgress(PACKET_ID, TransferPacketOutcome.SUCCESS)),
        ("TransferProgressSnapshot", TransferProgressSnapshot(TRANSFER_ID, SEQUENTIAL_ID, 4, 1, 3)),
        ("LinkTransferControlRequest", LinkTransferControlRequest(TransferControlKind.PROGRESS, TRANSFER_ID, SESSION_HANDLE, (TransferPacketProgress(PACKET_ID, TransferPacketOutcome.SUCCESS),), CORRELATION)),
        ("LinkTransferControlResult", LinkTransferControlResult(TransferControlKind.PROGRESS, (TransferProgressSnapshot(TRANSFER_ID, SEQUENTIAL_ID, 4, 1, 3),), (), ())),
        ("TbbAssemblyControlRequest", TbbAssemblyControlRequest(TbbAssemblyControlKind.ABORT, tbb_key, None, CORRELATION)),
        ("TbbAssemblyControlResult", TbbAssemblyControlResult(TbbAssemblyControlKind.ABORT, (tbb_key,), 1, 1, 0)),
    )


def public_dto_samples() -> tuple:
    """(이름, instance) 목록. 모든 공개 DTO를 포함한다."""

    return (
        # common
        ("Address.addressed", Addressed(STATION)),
        ("Address.broadcast", Broadcast()),
        ("OriginalSource", OriginalSource(STATION)),
        ("OriginalDestination", OriginalDestination(Addressed(PEER))),
        ("NodeSource", NodeSource(STATION)),
        ("NodeDestination", NodeDestination(Broadcast())),
        ("RouteAddresses", ROUTE),
        ("TimePoint.utc", UTC),
        ("TimePoint.monotonic", MONOTONIC),
        ("TimePoint.radio", HARDWARE),
        (
            "ClockMapping",
            ClockMapping(
                source_domain=ClockDomain.UTC,
                source_clock_id="test-utc",
                target_domain=ClockDomain.RADIO_HARDWARE,
                target_clock_id="test-radio",
                source_reference=UTC,
                target_reference=HARDWARE,
                uncertainty_nanoseconds=100,
            ),
        ),
        ("Details", DETAILS),
        ("Correlation", CORRELATION),
        ("InternalProtocolError", INTERNAL_ERROR),
        ("ServiceError", SERVICE_ERROR),
        ("EventPublished", EventPublished(event_id="event-1")),
        (
            "InternalEvent",
            InternalEvent(
                event_id="event-2",
                event_type=InternalEventType.PACKETS_RECOVERED,
                occurred_at=UTC,
                severity=EventSeverity.INFO,
                layer=Layer.PHY,
                correlation=CORRELATION,
                payload=RECOVERED_SET,
                error=None,
                details=DETAILS,
            ),
        ),
        (
            "InternalEvent.error",
            InternalEvent(
                event_id="event-3",
                event_type=InternalEventType.LAYER_ERROR,
                occurred_at=UTC,
                severity=EventSeverity.ERROR,
                layer=Layer.LINK,
                correlation=CORRELATION,
                payload=None,
                error=INTERNAL_ERROR,
                details=Details(),
            ),
        ),
        (
            "ServiceEvent",
            ServiceEvent(
                event_id="event-4",
                event_type=ServiceEventType.DATA_RECEIVED,
                occurred_at=UTC,
                severity=EventSeverity.INFO,
                transfer_id=TRANSFER_ID,
                transfer_state=TransferState.COMPLETED,
                received_data=RECEIVED_APPLICATION_DATA,
                error=None,
                details=Details.of(reason="stage0"),
            ),
        ),
        # service
        (
            "ServiceStartRequest",
            ServiceStartRequest(local_station_id=STATION, role=ServiceRole.SHIP, config_ref="cfg"),
        ),
        (
            "ServiceStarted",
            ServiceStarted(
                endpoint_id="ep",
                role=ServiceRole.SHIP,
                local_station_id=STATION,
                health=EndpointHealth.RUNNING,
                started_at=UTC,
            ),
        ),
        ("ServiceStopRequest", ServiceStopRequest(reason="stop")),
        (
            "ServiceStopped",
            ServiceStopped(endpoint_id="ep", health=EndpointHealth.STOPPED, stopped_at=UTC),
        ),
        (
            "SubmitTransferRequest",
            SubmitTransferRequest(
                destination=Addressed(PEER),
                payload_format=PAYLOAD_FORMAT,
                payload=b"payload",
                ack_required=True,
            ),
        ),
        (
            "TransferAccepted",
            TransferAccepted(transfer_id=TRANSFER_ID, state=TransferState.QUEUED, accepted_at=UTC),
        ),
        ("CancelTransferRequest", CancelTransferRequest(transfer_id=TRANSFER_ID)),
        (
            "CancelAccepted",
            CancelAccepted(
                transfer_id=TRANSFER_ID, state=TransferState.CANCELLED, accepted_at=UTC
            ),
        ),
        ("TransferStatusRequest", TransferStatusRequest(transfer_id=TRANSFER_ID)),
        (
            "TransferStatusSnapshot",
            TransferStatusSnapshot(
                transfer_id=TRANSFER_ID,
                state=TransferState.FAILED,
                updated_at=UTC,
                error=SERVICE_ERROR,
            ),
        ),
        ("ReceivedApplicationData", RECEIVED_APPLICATION_DATA),
        # presentation
        (
            "PresentationEncodeRequest",
            PresentationEncodeRequest(
                transfer_id=TRANSFER_ID,
                route=ROUTE,
                payload_format=PAYLOAD_FORMAT,
                application_payload=b"payload",
                ack_required=False,
            ),
        ),
        ("PresentationData", PRESENTATION_DATA),
        (
            "PresentationDecodeRequest",
            PresentationDecodeRequest(route=ROUTE, data=b"payload", received_at=UTC),
        ),
        (
            "ApplicationData",
            ApplicationData(
                route=ROUTE,
                payload_format=PAYLOAD_FORMAT,
                application_payload=b"payload",
                received_at=UTC,
            ),
        ),
        # link and message
        (
            "LinkAnalyzeRequest",
            LinkAnalyzeRequest(
                transfer_id=TRANSFER_ID,
                route=ROUTE,
                ack_required=False,
                presentation_data=b"payload",
            ),
        ),
        (
            "TransferRequirement",
            TransferRequirement(
                transfer_id=TRANSFER_ID,
                route=ROUTE,
                ack_required=False,
                presentation_size=7,
                candidate=TransferKind.SHORT_DATA,
                sequential_id=SEQUENTIAL_ID,
            ),
        ),
        (
            "LinkBuildRequest",
            LinkBuildRequest(
                plan=TRANSMISSION_PLAN,
                presentation_data=b"payload",
                sequential_id=SEQUENTIAL_ID,
                retransmission_number=17,
            ),
        ),
        ("BuiltPacket", BUILT_PACKET),
        (
            "LinkDecodeRequest",
            LinkDecodeRequest(
                packet_id=PACKET_ID, link_id=LINK_ID, received_at=UTC, packet=b"packet"
            ),
        ),
        ("FragmentRef", FragmentRef(index=0, count=3)),
        ("DecodedMessage", DECODED_MESSAGE),
        ("DecodedMessages", DECODED_MESSAGES),
        *_stage1_dto_samples(),
        # mac
        (
            "RequirementAccepted",
            RequirementAccepted(transfer_id=TRANSFER_ID, accepted_at=UTC),
        ),
        ("TransmissionPlan", TRANSMISSION_PLAN),
        (
            "PacketDecodeAction",
            PacketDecodeAction(packet=RECOVERED_PACKET, accepted=True, reason_code=None),
        ),
        ("MacMessageInput", MacMessageInput(decoded=DECODED_MESSAGES, channel=CHANNEL)),
        (
            "MacAction",
            MacAction(
                kind=MacActionKind.DELIVER_PRESENTATION_DATA,
                packet_id=PACKET_ID,
                session_handle=SESSION_HANDLE,
                presentation_data=b"payload",
                route=ROUTE,
                details=Details.of(message_type=255),
            ),
        ),
        (
            "MacActionSet",
            MacActionSet(
                actions=(
                    MacAction(kind=MacActionKind.NO_ACTION, packet_id=PACKET_ID),
                )
            ),
        ),
        (
            "SessionControlRequest",
            SessionControlRequest(
                kind=SessionControlKind.CLOSE,
                session_handle=SESSION_HANDLE,
                transfer_id=TRANSFER_ID,
            ),
        ),
        (
            "SessionControlResult",
            SessionControlResult(
                kind=SessionControlKind.CLOSE, session_handle=SESSION_HANDLE, active=False
            ),
        ),
        # phy
        (
            "PhyEncodeRequest",
            PhyEncodeRequest(
                packet_id=PACKET_ID,
                link_id=LINK_ID,
                channel=CHANNEL,
                transmit_at=UTC,
                packet=b"packet",
            ),
        ),
        ("BasebandBurst", BASEBAND_BURST),
        (
            "PhyDecodeRequest",
            PhyDecodeRequest(channel=CHANNEL, samples=SAMPLES, allowed_link_ids=(LINK_ID,)),
        ),
        ("RecoveredPacket", RECOVERED_PACKET),
        ("RecoveredPacketSet", RECOVERED_SET),
        (
            "PhyTxCompleted",
            PhyTxCompleted(burst_id=BURST_ID, packet_id=PACKET_ID, completed_at=UTC),
        ),
        # radio
        ("SampleBlock", SAMPLES),
        ("RadioTimingConfiguration", RADIO_TIMING),
        ("RadioResourceLimits", RADIO_LIMITS),
        ("RadioChannelBinding", RADIO_BINDING),
        ("RadioB210StreamConfiguration", RadioB210StreamConfiguration(30_720_000, "fc32", "sc16")),
        ("RadioCapabilities", RadioCapabilities((SAMPLE_FORMAT_CF32LE,), 0, 0, 0, True, (RadioTimingProfile.INTERNAL_FREE_RUNNING,), RADIO_LIMITS)),
        ("RadioAppliedChannelSetting", RADIO_APPLIED_CHANNEL),
        ("RadioAppliedSettings", RADIO_APPLIED_SETTINGS),
        ("RadioTimingStatus", RADIO_TIMING_STATUS),
        ("RadioQueueStatus", RadioQueueStatus(CHANNEL, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
        ("RadioEventDeliveryStatus", RadioEventDeliveryStatus(False, 0, None, None)),
        ("RadioRxWindowCompleted", RadioRxWindowCompleted(1, CHANNEL, UTC, 500_000, 2, 2, MONOTONIC)),
        ("RadioChannelSetting", CHANNEL_SETTING),
        ("RadioConfiguration", RADIO_CONFIGURATION),
        ("RadioConfigureRequest", RadioConfigureRequest(configuration=RADIO_CONFIGURATION)),
        (
            "RadioConfigurationApplied",
            RadioConfigurationApplied(
                adapter_kind="FAKE",
                device_state=RadioDeviceState.CONFIGURED,
                channels=(CHANNEL.name,),
                sample_format=SAMPLE_FORMAT_TEST_IQ_INT16_LE,
            ),
        ),
        ("RadioStartRequest", RadioStartRequest(reason="start")),
        (
            "RadioStarted",
            RadioStarted(
                adapter_kind="FAKE", device_state=RadioDeviceState.RUNNING, started_at=UTC
            ),
        ),
        ("RadioStopRequest", RadioStopRequest(reason="stop")),
        (
            "RadioStopped",
            RadioStopped(
                adapter_kind="FAKE", device_state=RadioDeviceState.STOPPED, stopped_at=UTC
            ),
        ),
        (
            "RadioTxRequest",
            RadioTxRequest(
                burst_id=BURST_ID, channel=CHANNEL, transmit_at=UTC, samples=SAMPLES
            ),
        ),
        (
            "RadioTxAccepted",
            RadioTxAccepted(
                burst_id=BURST_ID, channel=CHANNEL, scheduled_at=UTC, accepted_at=UTC
            ),
        ),
        (
            "RadioTxCompleted",
            RadioTxCompleted(burst_id=BURST_ID, channel=CHANNEL, completed_at=UTC),
        ),
        (
            "RadioRxRequest",
            RadioRxRequest(
                channel=CHANNEL, start_at=UTC, duration_nanoseconds=1000, sample_rate_hz=4000
            ),
        ),
        (
            "RadioRxAccepted",
            RadioRxAccepted(
                channel=CHANNEL, start_at=UTC, duration_nanoseconds=1000, accepted_at=UTC
            ),
        ),
        (
            "RadioSamplesReceived",
            RadioSamplesReceived(
                channel=CHANNEL,
                samples=SAMPLES,
                hardware_received_at=HARDWARE,
                continuity=SAMPLES.continuity,
            ),
        ),
        ("RadioStatusRequest", RadioStatusRequest(channel=CHANNEL)),
        (
            "RadioStatusSnapshot",
            RadioStatusSnapshot(
                adapter_kind="FAKE",
                device_state=RadioDeviceState.RUNNING,
                clock_reference=ClockReference.INTERNAL,
                clock_locked=True,
                pps_locked=False,
                underflow_count=0,
                overflow_count=0,
                late_command_count=0,
                details=Details.of(reason="stage0"),
            ),
        ),
    )
