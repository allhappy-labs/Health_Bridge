"""Version 2 original HealthKit sample archive wire contract.

This module has no storage or Home Assistant dependencies. Call it only after
the webhook has authenticated the Health Assistant Link token and user scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ARCHIVE_PROTOCOL_VERSION = 2
ARCHIVE_SCHEMA_VERSION = 1
PAYLOAD_SCHEMA_VERSION = 1
MAX_ARCHIVE_BATCH_BYTES = 262_144
MAX_ARCHIVE_SAMPLES_PER_BATCH = 200
MAX_ARCHIVE_DELETIONS_PER_BATCH = 200
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_TYPE = re.compile(r"HK(?:Quantity|Category)TypeIdentifier[A-Za-z0-9]{1,96}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_REQUEST_TYPES = frozenset({"archive_capability", "archive_batch", "archive_status"})
_STATES = frozenset({"pending", "current", "failed"})


class ArchiveProtocolError(ValueError):
    """Safe validation failure; never includes sample values or metadata."""

    def __init__(self, code: str, field: str = "payload") -> None:
        self.code = code
        self.field = field
        super().__init__(f"{code}: {field}")


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_batch_bytes: int = MAX_ARCHIVE_BATCH_BYTES
    max_samples_per_batch: int = MAX_ARCHIVE_SAMPLES_PER_BATCH
    max_deletions_per_batch: int = MAX_ARCHIVE_DELETIONS_PER_BATCH
    max_metadata_bytes: int = 8_192
    max_string_length: int = 256
    max_anchor_length: int = 4_096

    def __post_init__(self) -> None:
        for field, ceiling in (
            ("max_batch_bytes", MAX_ARCHIVE_BATCH_BYTES),
            ("max_samples_per_batch", MAX_ARCHIVE_SAMPLES_PER_BATCH),
            ("max_deletions_per_batch", MAX_ARCHIVE_DELETIONS_PER_BATCH),
        ):
            value = getattr(self, field)
            if type(value) is not int or value > ceiling:
                raise ArchiveProtocolError("limit_exceeded", field)


@dataclass(frozen=True, slots=True)
class ArchiveCoverage:
    kind: str
    authorization_start: datetime | None
    start: datetime | None = None
    end: datetime | None = None
    anchor: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveSource:
    bundle_id: str
    name: str
    revision: str


@dataclass(frozen=True, slots=True)
class QuantityPayload:
    kind: str
    schema_version: int
    raw_value: float
    raw_unit: str
    canonical_value: float
    canonical_unit: str


@dataclass(frozen=True, slots=True)
class CategoryPayload:
    kind: str
    schema_version: int
    value: int


@dataclass(frozen=True, slots=True)
class WorkoutPayload:
    kind: str
    schema_version: int
    activity_type: str
    duration_seconds: float
    total_energy_json: str | None = None
    total_distance_json: str | None = None
    detail_json: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveSample:
    uuid: str
    start: datetime
    end: datetime
    source: ArchiveSource
    time_zone: str
    metadata_json: str
    payload: QuantityPayload | CategoryPayload | WorkoutPayload
    device_json: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveBatch:
    """Validated request; control requests have no sample fields."""

    request_type: str
    protocol_version: int
    request_id: str
    user_id: str
    batch_id: str | None = None
    sample_type: str | None = None
    coverage: ArchiveCoverage | None = None
    samples: tuple[ArchiveSample, ...] = ()
    deletions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchiveCapability:
    request_id: str
    archive_schema_version: int
    max_batch_bytes: int
    max_samples_per_batch: int
    max_deletions_per_batch: int
    supported_sample_types: tuple[str, ...]
    supported_metrics: tuple[str, ...]
    archive_available: bool
    statistics_available: bool

    @classmethod
    def from_dict(cls, value: Any) -> ArchiveCapability:
        obj = _object(value, "invalid_response", "capability")
        _keys(
            obj,
            {
                "ok",
                "request_type",
                "protocol_version",
                "request_id",
                "archive_schema_version",
                "max_batch_bytes",
                "max_samples_per_batch",
                "max_deletions_per_batch",
                "supported_sample_types",
                "supported_metrics",
                "archive_available",
                "statistics_available",
            },
            set(),
            "invalid_response",
            "capability",
        )
        _response_header(obj, "archive_capability")
        sample_types = _string_list(
            obj["supported_sample_types"],
            "supported_sample_types",
            lambda value, field: _sample_type(value, field, "invalid_response"),
        )
        metrics = _string_list(
            obj["supported_metrics"],
            "supported_metrics",
            lambda value, field: _id(value, field, "invalid_response"),
        )
        if not isinstance(obj["archive_available"], bool) or not isinstance(
            obj["statistics_available"], bool
        ):
            raise ArchiveProtocolError("invalid_response", "availability")
        return cls(
            request_id=_id(obj["request_id"], "request_id", "invalid_response"),
            archive_schema_version=_positive_int(
                obj["archive_schema_version"], "archive_schema_version"
            ),
            max_batch_bytes=_positive_int(
                obj["max_batch_bytes"], "max_batch_bytes", MAX_ARCHIVE_BATCH_BYTES
            ),
            max_samples_per_batch=_positive_int(
                obj["max_samples_per_batch"],
                "max_samples_per_batch",
                MAX_ARCHIVE_SAMPLES_PER_BATCH,
            ),
            max_deletions_per_batch=_positive_int(
                obj["max_deletions_per_batch"],
                "max_deletions_per_batch",
                MAX_ARCHIVE_DELETIONS_PER_BATCH,
            ),
            supported_sample_types=sample_types,
            supported_metrics=metrics,
            archive_available=obj["archive_available"],
            statistics_available=obj["statistics_available"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "request_type": "archive_capability",
            "protocol_version": 2,
            "request_id": self.request_id,
            "archive_schema_version": self.archive_schema_version,
            "max_batch_bytes": self.max_batch_bytes,
            "max_samples_per_batch": self.max_samples_per_batch,
            "max_deletions_per_batch": self.max_deletions_per_batch,
            "supported_sample_types": list(self.supported_sample_types),
            "supported_metrics": list(self.supported_metrics),
            "archive_available": self.archive_available,
            "statistics_available": self.statistics_available,
        }


@dataclass(frozen=True, slots=True)
class ArchiveReceipt:
    request_id: str
    batch_id: str
    received_samples: int
    committed_samples: int
    received_deletions: int
    committed_deletions: int
    projection_state: str

    @classmethod
    def from_dict(cls, value: Any) -> ArchiveReceipt:
        obj = _object(value, "invalid_response", "ack")
        _keys(
            obj,
            {
                "ok",
                "archive_commit",
                "protocol_version",
                "request_id",
                "batch_id",
                "received_samples",
                "committed_samples",
                "received_deletions",
                "committed_deletions",
                "projection_state",
            },
            set(),
            "invalid_response",
            "ack",
        )
        if (
            obj["ok"] is not True
            or obj["archive_commit"] != "committed"
            or type(obj["protocol_version"]) is not int
            or obj["protocol_version"] != 2
        ):
            raise ArchiveProtocolError("invalid_response", "ack")
        counts = tuple(
            _nonnegative_int(obj[key], key, ceiling)
            for key, ceiling in (
                ("received_samples", MAX_ARCHIVE_SAMPLES_PER_BATCH),
                ("committed_samples", MAX_ARCHIVE_SAMPLES_PER_BATCH),
                ("received_deletions", MAX_ARCHIVE_DELETIONS_PER_BATCH),
                ("committed_deletions", MAX_ARCHIVE_DELETIONS_PER_BATCH),
            )
        )
        if counts[1] > counts[0] or counts[3] > counts[2]:
            raise ArchiveProtocolError("invalid_response", "counts")
        state = obj["projection_state"]
        if not isinstance(state, str) or state not in _STATES:
            raise ArchiveProtocolError("invalid_response", "projection_state")
        return cls(
            _id(obj["request_id"], "request_id", "invalid_response"),
            _id(obj["batch_id"], "batch_id", "invalid_response"),
            *counts,
            state,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "archive_commit": "committed",
            "protocol_version": 2,
            "request_id": self.request_id,
            "batch_id": self.batch_id,
            "received_samples": self.received_samples,
            "committed_samples": self.committed_samples,
            "received_deletions": self.received_deletions,
            "committed_deletions": self.committed_deletions,
            "projection_state": self.projection_state,
        }


@dataclass(frozen=True, slots=True)
class MetricProjectionStatus:
    metric: str
    state: str
    last_error: str | None


@dataclass(frozen=True, slots=True)
class ArchiveProjectionStatus:
    request_id: str
    metrics: tuple[MetricProjectionStatus, ...]

    @classmethod
    def from_dict(cls, value: Any) -> ArchiveProjectionStatus:
        obj = _object(value, "invalid_response", "status")
        _keys(
            obj,
            {"ok", "request_type", "protocol_version", "request_id", "metrics"},
            set(),
            "invalid_response",
            "status",
        )
        _response_header(obj, "archive_status")
        raw_metrics = obj["metrics"]
        if not isinstance(raw_metrics, list) or len(raw_metrics) > 256:
            raise ArchiveProtocolError("invalid_response", "metrics")
        metrics = []
        seen = set()
        for raw in raw_metrics:
            item = _object(raw, "invalid_response", "metric")
            _keys(
                item,
                {"metric", "state", "last_error"},
                set(),
                "invalid_response",
                "metric",
            )
            metric = _id(item["metric"], "metric", "invalid_response")
            state = item["state"]
            error = item["last_error"]
            if (
                metric in seen
                or not isinstance(state, str)
                or state not in _STATES
                or (
                    error is not None
                    and (not isinstance(error, str) or len(error) > 256)
                )
            ):
                raise ArchiveProtocolError("invalid_response", "metric")
            seen.add(metric)
            metrics.append(MetricProjectionStatus(metric, state, error))
        return cls(
            _id(obj["request_id"], "request_id", "invalid_response"), tuple(metrics)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "request_type": "archive_status",
            "protocol_version": 2,
            "request_id": self.request_id,
            "metrics": [
                {"metric": m.metric, "state": m.state, "last_error": m.last_error}
                for m in self.metrics
            ],
        }


def validate_archive_request(payload: Any, *, limits: ArchiveLimits) -> ArchiveBatch:
    """Validate an authenticated v2 request before any archive mutation."""
    obj = _object(payload, "invalid_request", "payload")
    try:
        size = len(
            json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ArchiveProtocolError("invalid_request", "payload") from exc
    if size > limits.max_batch_bytes:
        raise ArchiveProtocolError("limit_exceeded", "payload")
    return validate_archive_fields(obj, limits=limits)


def validate_archive_fields(payload: Any, *, limits: ArchiveLimits) -> ArchiveBatch:
    """Validate field structure and bounds after the transport size check.

    Internal storage callers use this to revalidate normalized dataclasses:
    canonical timestamps and numbers can be larger than their wire encodings.
    HTTP callers must use ``validate_archive_request`` to enforce byte limits.
    """
    obj = _object(payload, "invalid_request", "payload")
    request_type = obj.get("request_type")
    if not isinstance(request_type, str) or request_type not in _REQUEST_TYPES:
        raise ArchiveProtocolError("invalid_request", "request_type")
    if type(obj.get("protocol_version")) is not int or obj["protocol_version"] != 2:
        raise ArchiveProtocolError("unsupported_protocol", "protocol_version")
    common = {"request_type", "protocol_version", "request_id", "user_id"}
    batch_fields = {"batch_id", "sample_type", "coverage", "samples", "deletions"}
    _keys(
        obj,
        common | batch_fields if request_type == "archive_batch" else common,
        {"token"},
        "invalid_request",
        "payload",
    )
    if "token" in obj:
        _bounded_string(
            obj["token"], "token", limits.max_string_length, "invalid_request"
        )
    request_id = _id(obj["request_id"], "request_id")
    user_id = _id(obj["user_id"], "user_id")
    if request_type != "archive_batch":
        return ArchiveBatch(request_type, 2, request_id, user_id)

    batch_id = _id(obj["batch_id"], "batch_id")
    sample_type = _sample_type(obj["sample_type"], "sample_type")
    coverage = _coverage(obj["coverage"], limits)
    raw_samples = obj["samples"]
    raw_deletions = obj["deletions"]
    if not isinstance(raw_samples, list) or not isinstance(raw_deletions, list):
        raise ArchiveProtocolError("invalid_request", "samples/deletions")
    if (
        len(raw_samples) > limits.max_samples_per_batch
        or len(raw_deletions) > limits.max_deletions_per_batch
    ):
        raise ArchiveProtocolError("limit_exceeded", "samples/deletions")
    if not raw_samples and not raw_deletions:
        raise ArchiveProtocolError("invalid_request", "samples/deletions")
    samples = tuple(_sample(item, sample_type, limits) for item in raw_samples)
    deletions = tuple(_uuid(item) for item in raw_deletions)
    ids = [item.uuid for item in samples] + list(deletions)
    if len(ids) != len(set(ids)):
        raise ArchiveProtocolError("duplicate_id", "samples/deletions")
    return ArchiveBatch(
        request_type,
        2,
        request_id,
        user_id,
        batch_id,
        sample_type,
        coverage,
        samples,
        deletions,
    )


def _object(value: Any, code: str, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ArchiveProtocolError(code, field)
    return value


def _keys(
    obj: dict[str, Any], required: set[str], optional: set[str], code: str, field: str
) -> None:
    if not required <= obj.keys() or obj.keys() - required - optional:
        raise ArchiveProtocolError(code, field)


def _bounded_string(value: Any, field: str, length: int, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > length
        or any(ord(c) < 32 for c in value)
    ):
        raise ArchiveProtocolError(code, field)
    return value


def _id(value: Any, field: str, code: str = "invalid_request") -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ArchiveProtocolError(code, field)
    return value


def _sample_type(value: Any, field: str, code: str = "invalid_request") -> str:
    if not isinstance(value, str) or not (
        _TYPE.fullmatch(value) or value == "HKWorkoutTypeIdentifier"
    ):
        raise ArchiveProtocolError(code, field)
    return value


def _date(value: Any, code: str, field: str) -> datetime:
    if not isinstance(value, str) or not _UTC.fullmatch(value):
        raise ArchiveProtocolError(code, field)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError as exc:
        raise ArchiveProtocolError(code, field) from exc


def _coverage(value: Any, limits: ArchiveLimits) -> ArchiveCoverage:
    obj = _object(value, "invalid_coverage", "coverage")
    kind = obj.get("kind")
    if kind == "interval":
        _keys(
            obj,
            {"kind", "start", "end", "authorization_start"},
            set(),
            "invalid_coverage",
            "coverage",
        )
        start = _date(obj["start"], "invalid_coverage", "coverage.start")
        end = _date(obj["end"], "invalid_coverage", "coverage.end")
        if start >= end:
            raise ArchiveProtocolError("invalid_coverage", "coverage")
        auth = obj["authorization_start"]
        if auth is not None:
            auth = _date(auth, "invalid_coverage", "coverage.authorization_start")
        return ArchiveCoverage(kind, auth, start, end)
    if kind == "anchor":
        _keys(
            obj,
            {"kind", "anchor", "authorization_start"},
            set(),
            "invalid_coverage",
            "coverage",
        )
        anchor = _bounded_string(
            obj["anchor"],
            "coverage.anchor",
            limits.max_anchor_length,
            "invalid_coverage",
        )
        auth = obj["authorization_start"]
        if auth is not None:
            auth = _date(auth, "invalid_coverage", "coverage.authorization_start")
        return ArchiveCoverage(kind, auth, anchor=anchor)
    raise ArchiveProtocolError("invalid_coverage", "coverage.kind")


def _uuid(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ArchiveProtocolError("invalid_sample", "uuid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ArchiveProtocolError("invalid_sample", "uuid") from exc
    if str(parsed) != value.lower():
        raise ArchiveProtocolError("invalid_sample", "uuid")
    return str(parsed)


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArchiveProtocolError("invalid_sample", field)
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ArchiveProtocolError("invalid_sample", field) from exc
    if not math.isfinite(number):
        raise ArchiveProtocolError("invalid_sample", field)
    return number


def _json(value: Any, field: str, max_bytes: int) -> str:
    if not isinstance(value, dict):
        raise ArchiveProtocolError("invalid_sample", field)
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ArchiveProtocolError("invalid_sample", field) from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ArchiveProtocolError("limit_exceeded", field)
    return encoded


def _sample(value: Any, sample_type: str, limits: ArchiveLimits) -> ArchiveSample:
    obj = _object(value, "invalid_sample", "sample")
    _keys(
        obj,
        {"uuid", "start", "end", "source", "time_zone", "metadata", "payload"},
        {"device"},
        "invalid_sample",
        "sample",
    )
    start = _date(obj["start"], "invalid_sample", "sample.start")
    end = _date(obj["end"], "invalid_sample", "sample.end")
    if start > end:
        raise ArchiveProtocolError("invalid_sample", "sample.interval")
    source = _object(obj["source"], "invalid_sample", "source")
    _keys(source, {"bundle_id", "name", "revision"}, set(), "invalid_sample", "source")
    source_model = ArchiveSource(
        *(
            _bounded_string(
                source[key], "source." + key, limits.max_string_length, "invalid_sample"
            )
            for key in ("bundle_id", "name", "revision")
        )
    )
    zone = _bounded_string(obj["time_zone"], "time_zone", 128, "invalid_sample")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ArchiveProtocolError("invalid_sample", "time_zone") from exc
    metadata_json = _json(obj["metadata"], "metadata", limits.max_metadata_bytes)
    device_json = None
    if "device" in obj and obj["device"] is not None:
        device = _object(obj["device"], "invalid_sample", "device")
        _keys(
            device,
            set(),
            {"manufacturer", "model", "name", "hardware_version", "software_version"},
            "invalid_sample",
            "device",
        )
        for key, item in device.items():
            _bounded_string(
                item, "device." + key, limits.max_string_length, "invalid_sample"
            )
        device_json = _json(device, "device", limits.max_metadata_bytes)
    payload = _typed_payload(obj["payload"], sample_type, limits)
    return ArchiveSample(
        _uuid(obj["uuid"]),
        start,
        end,
        source_model,
        zone,
        metadata_json,
        payload,
        device_json,
    )


def _typed_payload(
    value: Any, sample_type: str, limits: ArchiveLimits
) -> QuantityPayload | CategoryPayload | WorkoutPayload:
    obj = _object(value, "invalid_sample", "payload")
    kind = (
        "quantity"
        if sample_type.startswith("HKQuantity")
        else "category"
        if sample_type.startswith("HKCategory")
        else "workout"
    )
    if (
        obj.get("kind") != kind
        or type(obj.get("schema_version")) is not int
        or obj["schema_version"] != PAYLOAD_SCHEMA_VERSION
    ):
        raise ArchiveProtocolError("invalid_sample", "payload.kind/schema_version")
    if kind == "quantity":
        _keys(
            obj,
            {
                "kind",
                "schema_version",
                "raw_value",
                "raw_unit",
                "canonical_value",
                "canonical_unit",
            },
            set(),
            "invalid_sample",
            "payload",
        )
        return QuantityPayload(
            kind,
            1,
            _finite(obj["raw_value"], "payload.raw_value"),
            _bounded_string(
                obj["raw_unit"],
                "payload.raw_unit",
                limits.max_string_length,
                "invalid_sample",
            ),
            _finite(obj["canonical_value"], "payload.canonical_value"),
            _bounded_string(
                obj["canonical_unit"],
                "payload.canonical_unit",
                limits.max_string_length,
                "invalid_sample",
            ),
        )
    if kind == "category":
        _keys(
            obj, {"kind", "schema_version", "value"}, set(), "invalid_sample", "payload"
        )
        if (
            type(obj["value"]) is not int
            or obj["value"] < 0
            or obj["value"] > 2_147_483_647
        ):
            raise ArchiveProtocolError("invalid_sample", "payload.value")
        return CategoryPayload(kind, 1, obj["value"])
    _keys(
        obj,
        {"kind", "schema_version", "activity_type", "duration_seconds"},
        {"total_energy", "total_distance", "detail"},
        "invalid_sample",
        "payload",
    )
    activity = _bounded_string(
        obj["activity_type"],
        "payload.activity_type",
        limits.max_string_length,
        "invalid_sample",
    )
    duration = _finite(obj["duration_seconds"], "payload.duration_seconds")
    if duration < 0:
        raise ArchiveProtocolError("invalid_sample", "payload.duration_seconds")
    energy = _workout_quantity(obj, "total_energy", limits)
    distance = _workout_quantity(obj, "total_distance", limits)
    detail = (
        _json(obj["detail"], "payload.detail", limits.max_metadata_bytes)
        if "detail" in obj
        else None
    )
    return WorkoutPayload(kind, 1, activity, duration, energy, distance, detail)


def _workout_quantity(
    obj: dict[str, Any], key: str, limits: ArchiveLimits
) -> str | None:
    if key not in obj:
        return None
    quantity = _object(obj[key], "invalid_sample", "payload." + key)
    _keys(quantity, {"value", "unit"}, set(), "invalid_sample", "payload." + key)
    _finite(quantity["value"], "payload." + key + ".value")
    _bounded_string(
        quantity["unit"],
        "payload." + key + ".unit",
        limits.max_string_length,
        "invalid_sample",
    )
    return _json(quantity, "payload." + key, limits.max_metadata_bytes)


def _response_header(obj: dict[str, Any], request_type: str) -> None:
    if (
        obj["ok"] is not True
        or obj["request_type"] != request_type
        or type(obj["protocol_version"]) is not int
        or obj["protocol_version"] != 2
    ):
        raise ArchiveProtocolError("invalid_response", request_type)


def _positive_int(value: Any, field: str, ceiling: int | None = None) -> int:
    if (
        type(value) is not int
        or value <= 0
        or (ceiling is not None and value > ceiling)
    ):
        raise ArchiveProtocolError("invalid_response", field)
    return value


def _nonnegative_int(value: Any, field: str, ceiling: int) -> int:
    if type(value) is not int or value < 0 or value > ceiling:
        raise ArchiveProtocolError("invalid_response", field)
    return value


def _string_list(value: Any, field: str, validator: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 256:
        raise ArchiveProtocolError("invalid_response", field)
    result = tuple(validator(item, field) for item in value)
    if len(result) != len(set(result)):
        raise ArchiveProtocolError("invalid_response", field)
    return result
