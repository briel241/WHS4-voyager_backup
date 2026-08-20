"""JSONL structured logging과 redaction (`stage0_baseline.md` §10).

machine log는 UTF-8 JSON Lines를 기본으로 한다. application payload, private key,
certificate 본문, secret과 raw baseband samples는 일반 log에 기록하지 않고 길이, 승인된
digest 또는 artifact 상대 경로만 남긴다.

내부 log는 전체 correlation을 유지하고, Client-facing event를 만들 때 `runtime`의
projection이 내부 식별자를 제거한다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .errors import (
    Correlation,
    Details,
    EMPTY_CORRELATION,
    EMPTY_DETAILS,
    FORBIDDEN_DETAIL_KEYS,
    InternalProtocolError,
    Layer,
)
from .events import EventSeverity, InternalEvent
from .exceptions import InvalidValueError
from .time import TimePoint

LOG_SCHEMA_VERSION = 1
REDACTED_MARKER = "[REDACTED]"

_REDACT_SUBSTRINGS = (
    "secret",
    "password",
    "passphrase",
    "private_key",
    "signing_key",
    "token",
    "credential",
)


def is_redacted_key(key: str) -> bool:
    """일반 log에 값을 기록하면 안 되는 key인지 판정한다."""

    lowered = key.lower()
    return lowered in FORBIDDEN_DETAIL_KEYS or any(
        token in lowered for token in _REDACT_SUBSTRINGS
    )


def payload_digest(data: bytes) -> str:
    """payload 자체 대신 기록하는 승인된 digest 표현."""

    if not isinstance(data, (bytes, bytearray)):
        raise InvalidValueError("payload_digest: bytes여야 합니다")
    return "sha256:" + hashlib.sha256(bytes(data)).hexdigest()


def redact_mapping(values: dict) -> dict:
    """log에 넣기 전에 금지 key와 raw binary 값을 제거한다."""

    redacted = {}
    for key, value in values.items():
        if is_redacted_key(str(key)):
            redacted[key] = REDACTED_MARKER
            continue
        if isinstance(value, (bytes, bytearray)):
            redacted[key] = {"length": len(value), "digest": payload_digest(bytes(value))}
            continue
        if isinstance(value, dict):
            redacted[key] = redact_mapping(value)
            continue
        if isinstance(value, (list, tuple)):
            redacted[key] = [
                REDACTED_MARKER if isinstance(item, (bytes, bytearray)) else item
                for item in value
            ]
            continue
        redacted[key] = value
    return redacted


@dataclass(frozen=True, slots=True)
class LogContext:
    """모든 record에 붙는 실행 context. 시험 실행에서는 `TC-*`와 seed도 포함한다."""

    endpoint_id: str
    role: str
    code_version: str
    config_fingerprint: str = ""
    registry_fingerprints: tuple[tuple[str, str], ...] = ()
    run_id: str = ""
    test_case_id: str = ""
    seed: int | None = None
    test_clock_id: str = ""

    def __post_init__(self) -> None:
        for name in ("endpoint_id", "role", "code_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise InvalidValueError(f"LogContext.{name}: 비어 있지 않은 문자열이어야 합니다")
        if not isinstance(self.registry_fingerprints, tuple):
            raise InvalidValueError("LogContext.registry_fingerprints: tuple이어야 합니다")
        for entry in self.registry_fingerprints:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise InvalidValueError(
                    "LogContext.registry_fingerprints: (name, fingerprint) tuple이어야 합니다"
                )
        if self.seed is not None and (isinstance(self.seed, bool) or not isinstance(self.seed, int)):
            raise InvalidValueError("LogContext.seed: 정수여야 합니다")

    def as_record_fields(self) -> dict:
        fields = {
            "endpoint_id": self.endpoint_id,
            "role": self.role,
            "code_version": self.code_version,
            "config_fingerprint": self.config_fingerprint,
            "registry_fingerprints": {name: value for name, value in self.registry_fingerprints},
        }
        if self.run_id:
            fields["run_id"] = self.run_id
        if self.test_case_id:
            fields["test_case_id"] = self.test_case_id
        if self.seed is not None:
            fields["seed"] = self.seed
        if self.test_clock_id:
            fields["test_clock_id"] = self.test_clock_id
        return fields


def correlation_fields(correlation: Correlation) -> dict:
    """correlation을 log field로 펼친다. 비어 있는 값은 생략한다."""

    fields = {}
    if correlation.transfer_id is not None:
        fields["transfer_id"] = correlation.transfer_id.value
    if correlation.session_handle is not None:
        fields["session_handle"] = correlation.session_handle.value
    if correlation.sequential_id is not None:
        fields["sequential_id"] = correlation.sequential_id.value
    if correlation.packet_id is not None:
        fields["packet_id"] = correlation.packet_id.value
    if correlation.fragment_index is not None:
        fields["fragment_index"] = correlation.fragment_index
    if correlation.fragment_count is not None:
        fields["fragment_count"] = correlation.fragment_count
    if correlation.burst_id is not None:
        fields["burst_id"] = correlation.burst_id.value
    return fields


def build_log_record(
    context: LogContext,
    *,
    timestamp: TimePoint,
    severity: EventSeverity,
    logger: str,
    layer: Layer,
    event_code: str,
    message: str,
    correlation: Correlation = EMPTY_CORRELATION,
    details: Details = EMPTY_DETAILS,
) -> dict:
    """JSONL record 하나를 만든다. 값은 항상 redaction을 거친다."""

    if not isinstance(timestamp, TimePoint):
        raise InvalidValueError("build_log_record.timestamp: TimePoint여야 합니다")
    if not isinstance(severity, EventSeverity):
        raise InvalidValueError("build_log_record.severity: EventSeverity여야 합니다")
    if not isinstance(layer, Layer):
        raise InvalidValueError("build_log_record.layer: Layer여야 합니다")
    if not isinstance(correlation, Correlation):
        raise InvalidValueError("build_log_record.correlation: Correlation이어야 합니다")
    if not isinstance(details, Details):
        raise InvalidValueError("build_log_record.details: Details여야 합니다")

    record = {
        "schema_version": LOG_SCHEMA_VERSION,
        "timestamp": {
            "seconds": timestamp.seconds,
            "nanoseconds": timestamp.nanoseconds,
            "clock_domain": timestamp.clock_domain.value,
            "clock_id": timestamp.clock_id,
        },
        "severity": severity.value,
        "logger": logger,
        "layer": layer.value,
        "event_code": event_code,
        "message": message,
    }
    record.update(context.as_record_fields())
    record.update(correlation_fields(correlation))
    if not details.is_empty():
        record["details"] = redact_mapping(details.as_dict())
    return record


def record_from_internal_event(context: LogContext, event: InternalEvent, logger: str) -> dict:
    """`InternalEvent`를 내부 log record로 만든다. 전체 correlation을 보존한다."""

    if not isinstance(event, InternalEvent):
        raise InvalidValueError("record_from_internal_event: InternalEvent여야 합니다")
    details = event.details
    event_code = event.event_type.value
    message = event.event_type.value
    if isinstance(event.error, InternalProtocolError):
        event_code = event.error.code
        message = event.error.message
        details = event.error.details
    record = build_log_record(
        context,
        timestamp=event.occurred_at,
        severity=event.severity,
        logger=logger,
        layer=event.layer,
        event_code=event_code,
        message=message,
        correlation=event.correlation,
        details=details,
    )
    record["event_id"] = event.event_id
    record["event_type"] = event.event_type.value
    if event.error is not None:
        record["error_category"] = event.error.category.value
        record["retryable"] = event.error.retryable
    return record


class JsonlLogger:
    """UTF-8 JSON Lines writer. human console 출력은 선택 기능이다."""

    __slots__ = ("_stream", "_context", "_logger_name", "_records")

    def __init__(self, stream, context: LogContext, logger_name: str = "vdeter") -> None:
        if not isinstance(context, LogContext):
            raise InvalidValueError("JsonlLogger.context: LogContext여야 합니다")
        self._stream = stream
        self._context = context
        self._logger_name = logger_name
        self._records = []

    @property
    def context(self) -> LogContext:
        return self._context

    def _write(self, record: dict) -> dict:
        self._records.append(record)
        if self._stream is not None:
            self._stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            flush = getattr(self._stream, "flush", None)
            if callable(flush):
                flush()
        return record

    def log(
        self,
        *,
        timestamp: TimePoint,
        severity: EventSeverity,
        layer: Layer,
        event_code: str,
        message: str,
        correlation: Correlation = EMPTY_CORRELATION,
        details: Details = EMPTY_DETAILS,
    ) -> dict:
        record = build_log_record(
            self._context,
            timestamp=timestamp,
            severity=severity,
            logger=self._logger_name,
            layer=layer,
            event_code=event_code,
            message=message,
            correlation=correlation,
            details=details,
        )
        return self._write(record)

    def log_internal_event(self, event: InternalEvent) -> dict:
        return self._write(record_from_internal_event(self._context, event, self._logger_name))

    def records(self) -> tuple[dict, ...]:
        return tuple(self._records)
