"""Canonical Stage 3 Radio fixtures shared by contract and integration tests."""

from __future__ import annotations

import struct

from vdeter.common.contracts.radio import (
    SAMPLE_FORMAT_CF32LE,
    ClockReference,
    RadioB210StreamConfiguration,
    RadioChannelBinding,
    RadioChannelSetting,
    RadioConfiguration,
    RadioResourceLimits,
    RadioTimingConfiguration,
    RadioTimingProfile,
    SampleBlock,
    SampleContinuity,
)
from vdeter.common.identifiers import BurstId, PhysicalChannel
from vdeter.common.time import ClockDomain, TimePoint

LOWER = PhysicalChannel("VDETER_LOWER_25_TEST")
UPPER = PhysicalChannel("VDETER_UPPER_25_TEST")


def burst_id(number: int) -> BurstId:
    return BurstId(f"00000000-0000-4000-8000-{number:012d}")


def timing() -> RadioTimingConfiguration:
    return RadioTimingConfiguration(
        RadioTimingProfile.INTERNAL_FREE_RUNNING,
        ClockReference.INTERNAL,
        ClockReference.INTERNAL,
        ClockReference.NONE,
        8,
        1_000_000_000,
        10_000_000,
    )


def limits(**overrides) -> RadioResourceLimits:
    values = dict(
        tx_queue_max_count=64,
        tx_queue_max_bytes=8_388_608,
        rx_window_max_count=64,
        rx_queue_max_count=64,
        rx_queue_max_bytes=8_388_608,
        stop_timeout_ns=5_000_000_000,
    )
    values.update(overrides)
    return RadioResourceLimits(**values)


def software_configuration(*, role: str = "SHIP", resource_limits=None) -> RadioConfiguration:
    directions = ((True, False), (True, True)) if role == "SHIP" else ((False, True), (True, False))
    settings = (
        RadioChannelSetting(LOWER, 157_237_500, 76_800, 0, 0),
        RadioChannelSetting(UPPER, 161_837_500, 76_800, 0, 0),
    )
    bindings = tuple(
        RadioChannelBinding(channel, 25_000, 25_000, tx, rx)
        for channel, (tx, rx) in zip((LOWER, UPPER), directions, strict=True)
    )
    return RadioConfiguration(
        "SOFTWARE", "", ClockReference.INTERNAL, settings, SAMPLE_FORMAT_CF32LE,
        timing(), resource_limits or limits(), bindings, None,
    )


def b210_configuration(*, role: str = "SHIP", serial: str = "TESTSERIAL") -> RadioConfiguration:
    directions = ((True, False), (True, True)) if role == "SHIP" else ((False, True), (True, False))
    settings = (
        RadioChannelSetting(LOWER, 157_237_500, 76_800, 0, 0),
        RadioChannelSetting(UPPER, 161_837_500, 76_800, 0, 0),
    )
    bindings = []
    for channel, (tx, rx) in zip((LOWER, UPPER), directions, strict=True):
        bindings.append(RadioChannelBinding(
            channel, 25_000, 200_000, tx, rx,
            0 if tx else None, 0 if rx else None,
            "TX/RX" if tx else None, "RX2" if rx else None,
        ))
    return RadioConfiguration(
        "B210", f"serial={serial}", ClockReference.INTERNAL, settings,
        SAMPLE_FORMAT_CF32LE, timing(), limits(), tuple(bindings),
        RadioB210StreamConfiguration(30_720_000, "fc32", "sc16"),
    )


def cf32_block(at: TimePoint, values=(0.25 + 0.5j, -0.25 - 0.5j)) -> SampleBlock:
    values = tuple(values)
    return SampleBlock(
        SAMPLE_FORMAT_CF32LE,
        76_800,
        len(values),
        b"".join(struct.pack("<ff", value.real, value.imag) for value in values),
        at,
        SampleContinuity.CONTIGUOUS,
    )


def utc(nanoseconds: int, clock_id: str = "radio-test-utc") -> TimePoint:
    return TimePoint.from_nanoseconds(nanoseconds, ClockDomain.UTC, clock_id)


def software_v2_toml(*, role="SHIP", reverse_channels=False) -> str:
    directions = {
        "SHIP": (("true", "false"), ("true", "true")),
        "CONTROL": (("false", "true"), ("true", "false")),
    }[role]
    channels = [
        ("VDETER_LOWER_25_TEST", 157237500, directions[0]),
        ("VDETER_UPPER_25_TEST", 161837500, directions[1]),
    ]
    if reverse_channels:
        channels.reverse()
    channel_text = "\n".join(
        f'''[[radio.channels]]
name = "{name}"
center_frequency_hz = {frequency}
sample_rate_hz = 76800
tx_gain_db = 0
rx_gain_db = 0
waveform_bandwidth_hz = 25000
analog_bandwidth_hz = 25000
tx_enabled = {direction[0]}
rx_enabled = {direction[1]}
'''
        for name, frequency, direction in channels
    )
    return f'''schema_version = 2
[endpoint]
id = "radio-stage3"
role = "{role}"
station_id = 111111111
scope = "TEST"
code_version = "0.1.0"
[service]
max_payload_bytes = 1024
max_concurrent_transfers = 4
[link]
supported_link_ids = [11]
[mac]
link_id = 11
tx_channel = "VDETER_UPPER_25_TEST"
rx_channel = "VDETER_UPPER_25_TEST"
transmit_lead_nanoseconds = 0
[phy]
sample_rate_hz = 76800
[radio]
adapter = "SOFTWARE"
clock_reference = "INTERNAL"
sample_format = "cf32le"
[radio.timing]
profile = "INTERNAL_FREE_RUNNING"
clock_source = "INTERNAL"
time_source = "INTERNAL"
pps_source = "NONE"
mapping_attempt_count = 8
mapping_valid_for_nanoseconds = 1000000000
max_mapping_uncertainty_ns = 10000000
[radio.resource_limits]
tx_queue_max_count = 64
tx_queue_max_bytes = 8388608
rx_window_max_count = 64
rx_queue_max_count = 64
rx_queue_max_bytes = 8388608
stop_timeout_ns = 5000000000
{channel_text}
[sources]
time = "TEST"
random = "SEEDED"
id = "DETERMINISTIC"
seed = 3
[test]
peer_station_id = 222222222
'''
