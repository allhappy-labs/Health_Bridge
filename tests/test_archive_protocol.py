"""Archive v2 wire contract shared with the iOS importer."""

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


FIXTURES = Path(__file__).parents[1] / "docs/protocol/fixtures"


def request(request_type="archive_batch"):
    base = {
        "request_type": request_type,
        "protocol_version": 2,
        "request_id": "request-001",
        "user_id": "person-1",
    }
    if request_type == "archive_batch":
        base.update(
            {
                "batch_id": "batch-001",
                "sample_type": "HKQuantityTypeIdentifierStepCount",
                "coverage": {
                    "kind": "interval",
                    "start": "2024-01-01T00:00:00Z",
                    "end": "2024-01-02T00:00:00Z",
                    "authorization_start": "2023-01-01T00:00:00Z",
                },
                "samples": [
                    {
                        "uuid": "bd085ccc-22f4-4e80-a865-149bb5b0d1d4",
                        "start": "2024-01-01T10:00:00Z",
                        "end": "2024-01-01T10:00:01Z",
                        "source": {
                            "bundle_id": "com.example.watch",
                            "name": "Example Watch",
                            "revision": "1.0",
                        },
                        "time_zone": "Europe/Zurich",
                        "metadata": {"HKMetadataKeyWasUserEntered": False},
                        "payload": {
                            "kind": "quantity",
                            "schema_version": 1,
                            "raw_value": 12,
                            "raw_unit": "count",
                            "canonical_value": 12,
                            "canonical_unit": "count",
                        },
                    }
                ],
                "deletions": [],
            }
        )
    return base


def parse(payload, **limit_changes):
    from custom_components.health_bridge.archive_protocol import (
        ArchiveLimits,
        validate_archive_request,
    )

    return validate_archive_request(payload, limits=ArchiveLimits(**limit_changes))


@pytest.mark.parametrize("request_type", ["archive_capability", "archive_status"])
def test_control_requests_require_v2_and_do_not_carry_archive_data(request_type):
    parsed = parse(request(request_type))
    assert parsed.request_type == request_type
    assert parsed.protocol_version == 2
    assert parsed.user_id == "person-1"
    assert parsed.samples == ()

    invalid = request(request_type)
    invalid["protocol_version"] = 1
    with pytest.raises(ValueError, match="unsupported_protocol"):
        parse(invalid)

    invalid = request(request_type)
    invalid["samples"] = []
    with pytest.raises(ValueError, match="invalid_request"):
        parse(invalid)


def test_quantity_batch_normalizes_immutable_records_and_interval_coverage():
    parsed = parse(request())
    assert parsed.batch_id == "batch-001"
    assert parsed.sample_type == "HKQuantityTypeIdentifierStepCount"
    assert parsed.coverage.kind == "interval"
    assert parsed.coverage.start.isoformat() == "2024-01-01T00:00:00+00:00"
    assert parsed.samples[0].payload.raw_value == 12.0
    assert parsed.samples[0].metadata_json == '{"HKMetadataKeyWasUserEntered":false}'
    with pytest.raises(FrozenInstanceError):
        parsed.batch_id = "changed"


def test_category_and_workout_payloads_must_match_sample_type():
    category = request()
    category["sample_type"] = "HKCategoryTypeIdentifierSleepAnalysis"
    category["samples"][0]["payload"] = {
        "kind": "category",
        "schema_version": 1,
        "value": 3,
    }
    assert parse(category).samples[0].payload.value == 3

    workout = request()
    workout["sample_type"] = "HKWorkoutType"
    workout["samples"][0]["payload"] = {
        "kind": "workout",
        "schema_version": 1,
        "activity_type": "HKWorkoutActivityTypeRunning",
        "duration_seconds": 1800,
    }
    assert parse(workout).samples[0].payload.duration_seconds == 1800.0

    workout["samples"][0]["payload"]["kind"] = "quantity"
    with pytest.raises(ValueError, match="invalid_sample"):
        parse(workout)


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda p: p.update(protocol_version=True), "unsupported_protocol"),
        (lambda p: p.update(request_type="backfill"), "invalid_request"),
        (lambda p: p.update(request_id="x" * 65), "invalid_request"),
        (lambda p: p.update(user_id=""), "invalid_request"),
        (lambda p: p.update(batch_id="x" * 65), "invalid_request"),
        (lambda p: p.update(sample_type="steps"), "invalid_request"),
        (
            lambda p: p["coverage"].update(end="2023-01-01T00:00:00Z"),
            "invalid_coverage",
        ),
        (
            lambda p: p["coverage"].update(start="2024-01-01T00:00:00+01:00"),
            "invalid_coverage",
        ),
        (lambda p: p["samples"][0].update(uuid="bad"), "invalid_sample"),
        (
            lambda p: p["samples"][0].update(start="2024-01-01T10:00:00+01:00"),
            "invalid_sample",
        ),
        (
            lambda p: p["samples"][0]["payload"].update(raw_value=float("nan")),
            "invalid_sample",
        ),
        (
            lambda p: p["samples"][0]["payload"].update(canonical_value=float("inf")),
            "invalid_sample",
        ),
        (lambda p: p["samples"][0]["source"].update(name="x" * 257), "invalid_sample"),
        (lambda p: p["samples"][0].update(time_zone="not/a/zone"), "invalid_sample"),
        (
            lambda p: p["samples"][0].update(metadata={"secret": "x" * 9000}),
            "limit_exceeded",
        ),
    ],
)
def test_malformed_batches_are_rejected_before_storage(change, code):
    payload = request()
    change(payload)
    with pytest.raises(ValueError, match=code):
        parse(payload)


def test_counts_bytes_and_uuid_uniqueness_are_bounded():
    payload = request()
    payload["samples"].append(dict(payload["samples"][0]))
    with pytest.raises(ValueError, match="duplicate_id"):
        parse(payload)

    payload = request()
    payload["deletions"] = [payload["samples"][0]["uuid"]]
    with pytest.raises(ValueError, match="duplicate_id"):
        parse(payload)

    payload = request()
    with pytest.raises(ValueError, match="limit_exceeded"):
        parse(payload, max_samples_per_batch=0)
    with pytest.raises(ValueError, match="limit_exceeded"):
        parse(payload, max_batch_bytes=100)


def test_anchor_coverage_can_carry_deletions_without_samples():
    payload = request()
    payload["coverage"] = {
        "kind": "anchor",
        "anchor": "opaque-anchor-position",
        "authorization_start": "2023-01-01T00:00:00Z",
    }
    payload["deletions"] = [payload["samples"][0]["uuid"]]
    payload["samples"] = []
    parsed = parse(payload)
    assert parsed.coverage.anchor == "opaque-anchor-position"
    assert parsed.deletions == ("bd085ccc-22f4-4e80-a865-149bb5b0d1d4",)


def test_malformed_json_shapes_return_stable_errors_instead_of_type_errors():
    from custom_components.health_bridge.archive_protocol import (
        ArchiveProjectionStatus,
        ArchiveProtocolError,
        ArchiveReceipt,
    )

    malformed_request = request()
    malformed_request["request_type"] = []
    with pytest.raises(ArchiveProtocolError, match="invalid_request"):
        parse(malformed_request)

    malformed_ack = json.loads((FIXTURES / "archive-ack-v2.json").read_text())
    malformed_ack["projection_state"] = []
    with pytest.raises(ArchiveProtocolError, match="invalid_response"):
        ArchiveReceipt.from_dict(malformed_ack)

    malformed_status = json.loads((FIXTURES / "archive-status-v2.json").read_text())[
        "response"
    ]
    malformed_status["metrics"][0]["state"] = []
    with pytest.raises(ArchiveProtocolError, match="invalid_response"):
        ArchiveProjectionStatus.from_dict(malformed_status)


def test_extreme_quantity_number_is_rejected_as_invalid_sample():
    payload = request()
    payload["samples"][0]["payload"]["raw_value"] = 10**400
    with pytest.raises(ValueError, match="invalid_sample"):
        parse(payload)


@pytest.mark.parametrize(
    "field,above_ceiling",
    [
        ("max_batch_bytes", 262_145),
        ("max_samples_per_batch", 201),
        ("max_deletions_per_batch", 201),
    ],
)
def test_capability_rejects_advertised_limits_above_protocol_ceilings(
    field, above_ceiling
):
    from custom_components.health_bridge.archive_protocol import ArchiveCapability

    response = json.loads((FIXTURES / "archive-capability-v2.json").read_text())[
        "response"
    ]
    response[field] = above_ceiling
    with pytest.raises(ValueError, match="invalid_response"):
        ArchiveCapability.from_dict(response)


@pytest.mark.parametrize(
    "field,above_ceiling",
    [
        ("max_batch_bytes", 262_145),
        ("max_samples_per_batch", 201),
        ("max_deletions_per_batch", 201),
    ],
)
def test_configured_limits_cannot_raise_protocol_ceilings(field, above_ceiling):
    from custom_components.health_bridge.archive_protocol import ArchiveLimits

    with pytest.raises(ValueError, match="limit_exceeded"):
        ArchiveLimits(**{field: above_ceiling})


@pytest.mark.parametrize(
    "field,companion",
    [
        ("received_samples", None),
        ("committed_samples", "received_samples"),
        ("received_deletions", None),
        ("committed_deletions", "received_deletions"),
    ],
)
def test_receipt_rejects_counts_above_per_batch_ceiling(field, companion):
    from custom_components.health_bridge.archive_protocol import ArchiveReceipt

    response = json.loads((FIXTURES / "archive-ack-v2.json").read_text())
    response[field] = 201
    if companion:
        response[companion] = 201
    with pytest.raises(ValueError, match="invalid_response"):
        ArchiveReceipt.from_dict(response)


def test_receipt_and_capability_and_status_fixtures_round_trip_through_parsers():
    from custom_components.health_bridge.archive_protocol import (
        ArchiveCapability,
        ArchiveProjectionStatus,
        ArchiveReceipt,
    )

    capability = json.loads((FIXTURES / "archive-capability-v2.json").read_text())
    assert parse(capability["request"]).request_type == "archive_capability"
    assert (
        ArchiveCapability.from_dict(capability["response"]).as_dict()
        == capability["response"]
    )
    assert capability["response"]["archive_available"] is True

    batch = json.loads((FIXTURES / "archive-batch-v2.json").read_text())
    assert parse(batch).samples[0].payload.kind == "quantity"

    ack = json.loads((FIXTURES / "archive-ack-v2.json").read_text())
    assert ArchiveReceipt.from_dict(ack).as_dict() == ack
    assert ack["archive_commit"] == "committed"
    assert ack["projection_state"] == "pending"

    status = json.loads((FIXTURES / "archive-status-v2.json").read_text())
    assert parse(status["request"]).request_type == "archive_status"
    assert (
        ArchiveProjectionStatus.from_dict(status["response"]).as_dict()
        == status["response"]
    )
