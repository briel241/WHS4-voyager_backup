"""Immutable fixture-validation manifest for attenuated B210 tests."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ...common.exceptions import InvalidValueError


FIXTURE_PREFLIGHT_VERSION = "0.2.3"
NOMINAL_30DB_CONDUCTED_PROFILE = "USER_ACCEPTED_NOMINAL_30DB_CONDUCTED_V1"
NOMINAL_30DB_RISK_ACCEPTANCE_ID = (
    "VDETER-STAGE3-USER-ACCEPTED-NOMINAL-30DB-CONDUCTED-001"
)
NOMINAL_30DB_SERIALS = frozenset(("34D03DD", "34D6357"))
NOMINAL_30DB_COMPONENT_BY_TX_SERIAL = {
    "34D03DD": "ATT-A-30DB-2W-6GHZ-UNSERIALIZED",
    "34D6357": "ATT-B-30DB-2W-6GHZ-UNSERIALIZED",
}


class LossEvidence(str, Enum):
    MANUFACTURER_GUARANTEE = "MANUFACTURER_GUARANTEE"
    MEASUREMENT = "MEASUREMENT"
    USER_ACCEPTED_UNVERIFIED_LABEL = "USER_ACCEPTED_UNVERIFIED_LABEL"


class TxPowerEvidence(str, Enum):
    MANUFACTURER_MAXIMUM = "MANUFACTURER_MAXIMUM"
    CALIBRATED_MEASUREMENT = "CALIBRATED_MEASUREMENT"
    USER_ACCEPTED_UNCALIBRATED = "USER_ACCEPTED_UNCALIBRATED"


class FixtureValidationProfile(str, Enum):
    CALIBRATED_PROTECTION = "CALIBRATED_PROTECTION"
    USER_ACCEPTED_NOMINAL_30DB_CONDUCTED = NOMINAL_30DB_CONDUCTED_PROFILE


@dataclass(frozen=True, slots=True)
class FixtureComponent:
    component_id: str
    kind: str
    nominal_loss_millidb: int
    tolerance_millidb: int
    guaranteed_min_loss_millidb: int
    loss_evidence: LossEvidence
    minimum_frequency_hz: int
    maximum_frequency_hz: int
    maximum_input_millidbm: int

    def __post_init__(self) -> None:
        for name in ("component_id", "kind"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise InvalidValueError(f"FixtureComponent.{name}가 비어 있습니다")
        for name in (
            "nominal_loss_millidb", "tolerance_millidb", "guaranteed_min_loss_millidb",
            "minimum_frequency_hz", "maximum_frequency_hz", "maximum_input_millidbm",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise InvalidValueError(f"FixtureComponent.{name}는 정수여야 합니다")
        if self.nominal_loss_millidb < 0 or self.tolerance_millidb < 0 or self.guaranteed_min_loss_millidb < 0:
            raise InvalidValueError("fixture loss 값은 0 이상이어야 합니다")
        if self.guaranteed_min_loss_millidb > self.nominal_loss_millidb:
            raise InvalidValueError("보증 최소 손실은 명목 손실보다 클 수 없습니다")
        if self.guaranteed_min_loss_millidb > (
            self.nominal_loss_millidb - self.tolerance_millidb
        ):
            raise InvalidValueError(
                "보증 최소 손실은 명목 손실에서 허용오차를 뺀 값 이하여야 합니다"
            )
        minimum_unknown_by_acceptance = (
            self.loss_evidence is LossEvidence.USER_ACCEPTED_UNVERIFIED_LABEL
            and self.minimum_frequency_hz == 0
        )
        if (
            (self.minimum_frequency_hz <= 0 and not minimum_unknown_by_acceptance)
            or self.maximum_frequency_hz < self.minimum_frequency_hz
        ):
            raise InvalidValueError("fixture component frequency rating이 잘못되었습니다")
        if not isinstance(self.loss_evidence, LossEvidence):
            raise InvalidValueError("FixtureComponent.loss_evidence가 잘못되었습니다")


@dataclass(frozen=True, slots=True)
class FixtureDirection:
    tx_serial: str
    tx_connector: str
    rx_serial: str
    rx_connector: str
    components: tuple[FixtureComponent, ...]
    p_tx_bound_millidbm: int
    approved_tx_gain_db: int
    tx_power_evidence: TxPowerEvidence = TxPowerEvidence.MANUFACTURER_MAXIMUM
    tx_power_calibration_path: str | None = None
    tx_power_calibration_sha256: str | None = None

    def __post_init__(self) -> None:
        for name in ("tx_serial", "tx_connector", "rx_serial", "rx_connector"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise InvalidValueError(f"FixtureDirection.{name}가 비어 있습니다")
        if self.tx_serial == self.rx_serial:
            raise InvalidValueError("fixture direction은 서로 다른 B210을 연결해야 합니다")
        if self.tx_connector != "TX/RX" or self.rx_connector != "RX2":
            raise InvalidValueError("fixture connector는 TX/RX -> RX2이어야 합니다")
        if not isinstance(self.components, tuple) or not self.components or any(
            not isinstance(value, FixtureComponent) for value in self.components
        ):
            raise InvalidValueError("FixtureDirection.components가 잘못되었습니다")
        for name in ("p_tx_bound_millidbm", "approved_tx_gain_db"):
            if isinstance(getattr(self, name), bool) or not isinstance(getattr(self, name), int):
                raise InvalidValueError(f"FixtureDirection.{name}는 정수여야 합니다")
        if not isinstance(self.tx_power_evidence, TxPowerEvidence):
            raise InvalidValueError("FixtureDirection.tx_power_evidence가 잘못되었습니다")
        calibrated = self.tx_power_evidence is TxPowerEvidence.CALIBRATED_MEASUREMENT
        user_accepted = (
            self.tx_power_evidence is TxPowerEvidence.USER_ACCEPTED_UNCALIBRATED
        )
        if calibrated:
            if (
                not isinstance(self.tx_power_calibration_path, str)
                or not self.tx_power_calibration_path
            ):
                raise InvalidValueError("교정된 TX power bound에는 calibration path가 필요합니다")
            digest = self.tx_power_calibration_sha256
            if (
                not isinstance(digest, str)
                or not digest.startswith("sha256:")
                or len(digest) != 71
                or any(value not in "0123456789abcdef" for value in digest[7:])
            ):
                raise InvalidValueError("TX power calibration SHA-256이 잘못되었습니다")
        elif user_accepted:
            if (
                self.p_tx_bound_millidbm != 20_000
                or self.approved_tx_gain_db != 0
                or self.tx_power_calibration_path is not None
                or self.tx_power_calibration_sha256 is not None
            ):
                raise InvalidValueError(
                    "사용자 수용 유선 profile은 +20 dBm 제조사 상한, gain 0 dB, "
                    "calibration 미사용이어야 합니다"
                )
        elif (
            self.p_tx_bound_millidbm != 20_000
            or self.tx_power_calibration_path is not None
            or self.tx_power_calibration_sha256 is not None
        ):
            raise InvalidValueError(
                "제조사 최대값 evidence는 +20 dBm bound와 calibration 미사용이어야 합니다"
            )

    @property
    def path_min_loss_millidb(self) -> int:
        return sum(value.guaranteed_min_loss_millidb for value in self.components)

    @property
    def p_rx_worst_millidbm(self) -> int:
        return self.p_tx_bound_millidbm - self.path_min_loss_millidb


@dataclass(frozen=True, slots=True)
class B210FixtureManifest:
    fixture_id: str
    source_radio_fingerprint: str
    receiver_radio_fingerprint: str
    center_frequencies_hz: tuple[int, ...]
    directions: tuple[FixtureDirection, ...]
    created_at_utc: str
    preflight_version: str
    outcome: str
    validation_profile: FixtureValidationProfile = (
        FixtureValidationProfile.CALIBRATED_PROTECTION
    )
    risk_acceptance_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "fixture_id", "source_radio_fingerprint", "receiver_radio_fingerprint",
            "created_at_utc", "preflight_version",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise InvalidValueError(f"B210FixtureManifest.{name}가 비어 있습니다")
        if self.outcome != "PASS":
            raise InvalidValueError("B210 fixture preflight outcome은 PASS여야 TX를 허용합니다")
        if self.preflight_version != FIXTURE_PREFLIGHT_VERSION:
            raise InvalidValueError(
                f"B210 fixture preflight version은 {FIXTURE_PREFLIGHT_VERSION}이어야 합니다"
            )
        if not isinstance(self.validation_profile, FixtureValidationProfile):
            raise InvalidValueError("B210 fixture validation profile이 잘못되었습니다")
        if (
            not isinstance(self.center_frequencies_hz, tuple)
            or set(self.center_frequencies_hz) != {157_237_500, 161_837_500}
        ):
            raise InvalidValueError("fixture manifest에는 lower/upper 중심 주파수가 필요합니다")
        if not isinstance(self.directions, tuple) or len(self.directions) != 2 or any(
            not isinstance(value, FixtureDirection) for value in self.directions
        ):
            raise InvalidValueError("fixture manifest에는 양방향 path 두 개가 필요합니다")
        pairs = {(value.tx_serial, value.rx_serial) for value in self.directions}
        nominal_user_profile = (
            self.validation_profile
            is FixtureValidationProfile.USER_ACCEPTED_NOMINAL_30DB_CONDUCTED
        )
        if len(pairs) != 2 or any((right, left) not in pairs for left, right in pairs):
            raise InvalidValueError("fixture path는 두 B210 사이의 양방향이어야 합니다")
        if nominal_user_profile:
            if self.risk_acceptance_id != NOMINAL_30DB_RISK_ACCEPTANCE_ID:
                raise InvalidValueError(
                    "명목 30 dB 유선 profile에는 고정 risk acceptance ID가 필요합니다"
                )
            observed_serials = {
                value for direction in self.directions
                for value in (direction.tx_serial, direction.rx_serial)
            }
            if observed_serials != NOMINAL_30DB_SERIALS:
                raise InvalidValueError(
                    "명목 30 dB 유선 profile은 승인된 두 B210 serial에만 적용됩니다"
                )
            component_ids = [
                component.component_id
                for direction in self.directions
                for component in direction.components
            ]
            if len(component_ids) != len(set(component_ids)):
                raise InvalidValueError("양방향 감쇠기는 서로 다른 component ID가 필요합니다")
        elif self.risk_acceptance_id is not None:
            raise InvalidValueError("교정 보호 profile에는 risk acceptance ID를 사용할 수 없습니다")
        for direction in self.directions:
            if nominal_user_profile:
                if (
                    direction.approved_tx_gain_db != 0
                    or direction.p_tx_bound_millidbm != 20_000
                    or direction.tx_power_evidence
                    is not TxPowerEvidence.USER_ACCEPTED_UNCALIBRATED
                    or len(direction.components) != 1
                ):
                    raise InvalidValueError(
                        "명목 30 dB 유선 profile은 gain 0 dB와 한 방향당 감쇠기 하나만 허용합니다"
                    )
                component = direction.components[0]
                if (
                    component.component_id
                    != NOMINAL_30DB_COMPONENT_BY_TX_SERIAL[direction.tx_serial]
                    or component.kind
                    != "SMA_FIXED_ATTENUATOR_AND_UNCHARACTERIZED_CABLE_PATH"
                    or component.nominal_loss_millidb != 30_000
                    or component.tolerance_millidb != 30_000
                    or component.guaranteed_min_loss_millidb != 0
                    or component.loss_evidence
                    is not LossEvidence.USER_ACCEPTED_UNVERIFIED_LABEL
                    or component.minimum_frequency_hz != 0
                    or component.maximum_frequency_hz != 6_000_000_000
                    or component.maximum_input_millidbm != 33_010
                ):
                    raise InvalidValueError(
                        "명목 30 dB 유선 profile component는 라벨의 30 dB/2 W/6 GHz와 "
                        "미검증 최소 감쇠를 그대로 기록해야 합니다"
                    )
            if not nominal_user_profile and direction.p_rx_worst_millidbm > -20_000:
                raise InvalidValueError("P_RX_WORST는 -20 dBm 이하여야 합니다")
            if (
                not nominal_user_profile
                and direction.path_min_loss_millidb < 40_000
                and direction.tx_power_evidence
                is not TxPowerEvidence.CALIBRATED_MEASUREMENT
            ):
                raise InvalidValueError(
                    "40 dB 미만 경로에는 교정된 TX power bound가 필요합니다"
                )
            for component in direction.components:
                if component.maximum_input_millidbm < direction.p_tx_bound_millidbm:
                    raise InvalidValueError("fixture component power rating이 TX bound보다 낮습니다")
                if any(
                    frequency < component.minimum_frequency_hz
                    or frequency > component.maximum_frequency_hz
                    for frequency in self.center_frequencies_hz
                ):
                    raise InvalidValueError("fixture component frequency rating이 시험 채널을 포함하지 않습니다")


def load_fixture_manifest(path) -> B210FixtureManifest:
    """Load strict JSON into an immutable manifest; unknown/missing fields fail."""

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_directions = data.pop("directions")
    center_frequencies_hz = tuple(data.pop("center_frequencies_hz"))
    data["validation_profile"] = FixtureValidationProfile(
        data.get("validation_profile", "CALIBRATED_PROTECTION")
    )
    directions = []
    for original_direction in raw_directions:
        raw_direction = dict(original_direction)
        components = tuple(
            FixtureComponent(
                **{**raw_component, "loss_evidence": LossEvidence(raw_component["loss_evidence"])}
            )
            for raw_component in raw_direction.pop("components")
        )
        raw_direction["tx_power_evidence"] = TxPowerEvidence(
            raw_direction.get("tx_power_evidence", "MANUFACTURER_MAXIMUM")
        )
        directions.append(FixtureDirection(**raw_direction, components=components))
    manifest = B210FixtureManifest(
        **data,
        center_frequencies_hz=center_frequencies_hz,
        directions=tuple(directions),
    )
    manifest_directory = Path(path).resolve().parent
    for direction in manifest.directions:
        if direction.tx_power_evidence is not TxPowerEvidence.CALIBRATED_MEASUREMENT:
            continue
        calibration_path = Path(direction.tx_power_calibration_path)
        if not calibration_path.is_absolute():
            calibration_path = manifest_directory / calibration_path
        if not calibration_path.is_file():
            raise InvalidValueError("TX power calibration artifact가 없습니다")
        actual = "sha256:" + hashlib.sha256(calibration_path.read_bytes()).hexdigest()
        if actual != direction.tx_power_calibration_sha256:
            raise InvalidValueError("TX power calibration artifact SHA-256이 다릅니다")
        _validate_tx_power_calibration(
            calibration_path, direction, manifest.center_frequencies_hz
        )
    return manifest


def _validate_tx_power_calibration(path, direction, center_frequencies_hz):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise InvalidValueError("TX power calibration artifact를 읽을 수 없습니다") from error
    required = {
        "schema_version", "outcome", "calibration_id", "device_serial",
        "tx_gain_db", "sample_format", "waveform_id", "measurement_plane",
        "measurement_device_id", "measured_at_utc", "measurements",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise InvalidValueError("TX power calibration artifact field가 잘못되었습니다")
    if value["schema_version"] != 1 or value["outcome"] != "PASS":
        raise InvalidValueError("TX power calibration artifact가 PASS가 아닙니다")
    for name in (
        "calibration_id", "device_serial", "sample_format", "waveform_id",
        "measurement_plane", "measurement_device_id", "measured_at_utc",
    ):
        if not isinstance(value[name], str) or not value[name]:
            raise InvalidValueError(f"TX power calibration {name}이 비어 있습니다")
    if value["device_serial"] != direction.tx_serial:
        raise InvalidValueError("TX power calibration serial이 fixture와 다릅니다")
    if value["tx_gain_db"] != direction.approved_tx_gain_db:
        raise InvalidValueError("TX power calibration gain이 fixture와 다릅니다")
    if value["sample_format"] != "cf32le":
        raise InvalidValueError("TX power calibration sample format이 잘못되었습니다")
    if value["waveform_id"] != "VDETER-STAGE2-LINK11-FROZEN-CF32LE":
        raise InvalidValueError("TX power calibration waveform이 잘못되었습니다")
    if value["measurement_plane"] != "B210_TX_RX_CONNECTOR_EQUIVALENT":
        raise InvalidValueError("TX power calibration measurement plane이 잘못되었습니다")
    measurements = value["measurements"]
    if not isinstance(measurements, list) or len(measurements) != 2:
        raise InvalidValueError("TX power calibration에는 두 주파수 측정이 필요합니다")
    expected_fields = {
        "frequency_hz", "measured_output_millidbm",
        "measurement_uncertainty_millidb", "conservative_bound_millidbm",
    }
    observed_frequencies = []
    conservative_bounds = []
    for measurement in measurements:
        if not isinstance(measurement, dict) or set(measurement) != expected_fields:
            raise InvalidValueError("TX power calibration measurement field가 잘못되었습니다")
        for name in expected_fields:
            item = measurement[name]
            if isinstance(item, bool) or not isinstance(item, int):
                raise InvalidValueError(f"TX power calibration {name}은 정수여야 합니다")
        if measurement["measurement_uncertainty_millidb"] < 0:
            raise InvalidValueError("TX power calibration uncertainty는 음수일 수 없습니다")
        calculated = (
            measurement["measured_output_millidbm"]
            + measurement["measurement_uncertainty_millidb"]
        )
        if measurement["conservative_bound_millidbm"] != calculated:
            raise InvalidValueError("TX power calibration conservative bound가 일치하지 않습니다")
        observed_frequencies.append(measurement["frequency_hz"])
        conservative_bounds.append(measurement["conservative_bound_millidbm"])
    if set(observed_frequencies) != set(center_frequencies_hz):
        raise InvalidValueError("TX power calibration 주파수가 fixture와 다릅니다")
    if max(conservative_bounds) > direction.p_tx_bound_millidbm:
        raise InvalidValueError("fixture TX power bound가 calibration 상한보다 낮습니다")
