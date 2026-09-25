# Health Bridge original-sample archive protocol v2

Version 2 is a separate, capability-advertised Health Assistant Link webhook
contract. It does not change live or backfill protocol v1, its 14-day limit, or
the existing Phone Assistant Link route. Authenticate the Health Assistant Link
token and bind the request to its Health Bridge `user_id` before calling
`validate_archive_request(payload, limits=ArchiveLimits(...))`. A `token` may be
present in the authenticated envelope; the parser discards it. This document
freezes the JSON shapes for the iOS importer. Storage and route behavior are
implemented by subsequent tasks.

The four canonical fixtures are in [`fixtures/`](fixtures/). The capability and
status fixtures contain one request and one response. The batch fixture is an
upload request, and the acknowledgement fixture is its response. Dates are
ISO-8601 UTC with a literal `Z` and at most six fractional digits. Other UTC
offset spellings are rejected. No secret appears in a fixture.

## Requests

All requests require `request_type`, integer `protocol_version: 2`, `request_id`,
and `user_id`. IDs are 1–64 ASCII letters, digits, `.`, `_`, or `-`, beginning
with a letter or digit. Unknown fields are rejected apart from an optional
authenticated-envelope `token`. `archive_capability` and `archive_status` have
no additional fields. Capability should be queried after connection setup and
when the integration version changes. Unsupported v2 leaves live v1 available.

`archive_batch` additionally requires `batch_id`, `sample_type`, `coverage`,
`samples`, and `deletions`. `sample_type` is one HealthKit quantity or category
type identifier (`HKQuantityTypeIdentifier…`, `HKCategoryTypeIdentifier…`) or
`HKWorkoutTypeIdentifier`. A batch contains one type only. The type must also
be in the authenticated server's advertised supported types. A batch has at
least one sample or deletion, at most 200 of each, and at most 262,144 bytes of
compact UTF-8 JSON. The route should also enforce the same limit on the raw
HTTP body before decoding; the parser caps normalized JSON. Each UUID is a
canonical 36-character UUID, unique across samples and deletions in one batch.
The archive's durable key is `(user_id, sample_type, uuid)`, so separate
same-time samples remain distinct. Retrying the same `batch_id` returns its
original receipt; storage handles duplicate UUIDs and content corrections.

`coverage` has exactly one of these shapes:

```json
{"kind":"interval","start":"2024-01-01T00:00:00Z","end":"2024-01-02T00:00:00Z","authorization_start":"2023-01-01T00:00:00Z"}
{"kind":"anchor","anchor":"opaque-anchor-position","authorization_start":null}
```

An interval is half-open `[start, end)`, with `start < end`. An anchor is a
nonempty opaque string of at most 4,096 characters. `authorization_start` is
the observed per-type HealthKit boundary for this scan, or `null` when the API
returns no boundary. `null` does not assert that read permission is granted or
denied. The sender advances a coverage interval or anchor only after checking
a committed acknowledgement for the same `request_id` and `batch_id`.

Each sample requires `uuid`, `start`, `end`, `source`, `time_zone`, `metadata`,
and a typed `payload`. `start <= end`. `source` contains nonempty `bundle_id`,
`name`, and `revision`; `time_zone` is an IANA name such as `Europe/Zurich` or
`UTC`. Optional `device` accepts `manufacturer`, `model`, `name`,
`hardware_version`, and `software_version`. Source and device strings are at
most 256 characters. `metadata` is a JSON object capped at 8,192 encoded
bytes; it holds only fields needed to reconstruct supported metric semantics.
No credential or opaque serialized HealthKit object belongs here.

The type-specific payload always has `schema_version: 1`:

| Kind | Required fields | Optional fields |
| --- | --- | --- |
| `quantity` | `raw_value`, `raw_unit`, `canonical_value`, `canonical_unit` | none |
| `category` | nonnegative integer `value` | none |
| `workout` | `activity_type`, nonnegative `duration_seconds` | `total_energy`, `total_distance` as `{ "value": finite_number, "unit": "..." }`; bounded JSON object `detail` |

Quantity and workout numbers must be finite; units and activity strings are
nonempty and at most 256 characters. A payload kind must match the batch's
sample type. Workout `detail` is limited to 8,192 encoded bytes. The sender
excludes samples written by this app or tagged with its Home Assistant origin
marker. Deletion UUIDs are strings in `deletions` and create tombstones after
the archive transaction commits.

## Responses and progress

Capability responds with `ok`, `request_type`, `protocol_version`, echoed
`request_id`, `archive_schema_version`, batch limits, supported sample types and
metric keys, and separate `archive_available` and `statistics_available`
booleans. The installed fork must populate the lists from its audited mapping;
the fixture's three entries illustrate the shape and do not claim complete
support for all 111 live metric keys. Advertised limits may be lower than the
protocol ceilings of 262,144 bytes, 200 samples, and 200 deletions, but never
higher.

A successful batch acknowledgement has `ok: true`,
`archive_commit: "committed"`, `protocol_version: 2`, echoed `request_id` and
`batch_id`, `received_samples`, `committed_samples`, `received_deletions`,
`committed_deletions`, and `projection_state` (`pending`, `current`, or
`failed`). Counts are nonnegative; committed counts do not exceed received
counts, and neither count exceeds the applicable 200-record batch ceiling. It
means the originals, tombstones, receipt, and projection work were
committed atomically. **It does not promise that Home Assistant statistics are
already visible.** The client reports samples archived separately from
statistics current and probes `archive_status` for per-metric `pending`,
`current`, or `failed` state and a bounded `last_error` string or `null`.
Projection failures are retried independently of the receipt.

## Errors

`ArchiveProtocolError.code` is stable and its message names a field, never a
health value. A webhook can expose `{ "ok": false, "error": "<code>" }` after
authentication. Validation fails before any archive mutation.

| Code | Meaning |
| --- | --- |
| `invalid_request` | Wrong request shape, ID, type identifier, or empty batch. |
| `unsupported_protocol` | Protocol version is not integer `2`. |
| `invalid_coverage` | Coverage shape, UTC date, interval, or anchor is invalid. |
| `invalid_sample` | UUID, provenance, timestamp, or typed payload is invalid. |
| `duplicate_id` | A UUID occurs twice in the batch or both as sample and deletion. |
| `limit_exceeded` | Batch count/bytes or metadata/detail bytes exceed advertised limits. |
| `invalid_response` | A locally parsed capability, acknowledgement, or status response violates v2. |

Authentication failure, unsupported advertised sample types, archive storage
failure, and projection failure are route/store concerns; they must not be
misreported as successful archive commits. Existing v1 error behavior is
unchanged.
