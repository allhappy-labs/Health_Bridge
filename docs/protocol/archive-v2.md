# Health Bridge original-sample archive protocol v2

Version 2 is a separate, capability-advertised Health Assistant Link webhook
contract. It does not change live or backfill protocol v1, its 14-day limit, or
the existing Phone Assistant Link route. Authenticate a registered Health
Assistant Link entry's token before calling
`validate_archive_request(payload, limits=ArchiveLimits(...))`. An entry with
an explicit `user_id` rejects requests for other users. Existing entries without
that setting retain their declared Health Bridge user namespace: the shared HAL
token authorizes all of that integration's users and is **not per-user
isolation**. Phone tokens and the untyped legacy YAML token cannot use archive
routes. A `token` may be present in the authenticated envelope; the parser
discards it. This document freezes the JSON shapes for the iOS importer.

The canonical wire fixtures and complete source catalog are in
[`fixtures/`](fixtures/). The capability and
status fixtures contain one request and one response. The batch fixture is an
upload request, and the acknowledgement fixture is its response. Dates are
ISO-8601 UTC with a literal `Z` and at most six fractional digits. Other UTC
offset spellings are rejected. Fixtures use a synthetic all-zero credential;
no live credential appears in a fixture.

## Requests

All requests require `request_type`, integer `protocol_version: 2`, `request_id`,
`user_id`, and `uploader_credential`. The credential is canonical unpadded
base64url of exactly 32 bytes (43 characters). It is stored only in the
phone's device-local Keychain and excluded from receipts, exports, responses,
and logs. IDs are 1–64 ASCII letters, digits, `.`, `_`, or `-`, beginning
with a letter or digit. Unknown fields are rejected apart from an optional
authenticated-envelope `token`. `archive_capability`, `archive_owner_claim`, and
`archive_status` have
no additional fields. Capability should be queried after connection setup and
when the integration version changes. Unsupported v2 leaves live v1 available.

Capability includes `ownership_contract_version: 1`, `owner_state`, and
`owner_generation`. The app must require this contract before version-2 import.
`owner_state` is `unbound`, `pending`, `active`, or `not_owner` relative to the
supplied credential. A pending claimant additionally receives `claim_id`,
`fingerprint`, and `expires_at`. The fingerprint is the first 12 lowercase hex
digits of SHA-256 over the decoded credential. One claim per Health Bridge user
may be pending for 24 hours; a retry by that phone is idempotent and a competing
phone receives HTTP 409 `owner_pending`. See
[`archive-owner-v2.json`](fixtures/archive-owner-v2.json).

An HA administrator reads the pending claim at
`GET /api/health_bridge/archive/{user_id}/owner`. Approval uses
`POST .../owner-approve` with exactly
`{"claim_id":"…","confirm_user_id":"person-1","confirm":"APPROVE"}`;
rejection uses `POST .../owner-reject` with `"REJECT"`. Bodies are bounded to
1,024 bytes. The administrator compares the claim fingerprint with the intended
phone before approval. Approval immediately revokes the prior phone and
increments generation; its old-phone-only originals remain archived. Back up
Home Assistant before transfer. No claim is approved automatically.

`archive_batch` additionally requires `batch_id`, `sample_type`, `coverage`,
`samples`, and `deletions`. `sample_type` is one HealthKit quantity or category
type identifier (`HKQuantityTypeIdentifier…`, `HKCategoryTypeIdentifier…`) or
`HKWorkoutType`. A batch contains one type only. The type must also
be in the authenticated server's advertised supported types. A batch has at
least one sample or deletion, at most 200 of each, and at most 262,144 bytes of
compact UTF-8 JSON. On the shared JSON-token webhook, Home Assistant's existing
raw HTTP body ceiling applies before decoding, including chunked requests.
After the envelope is decoded and authenticated, archive requests additionally
enforce the 262,144-byte raw body ceiling before schema parsing or mutation;
whitespace counts. This preserves v1's existing HTTP limit. The parser also
caps normalized JSON. Each UUID is a
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

### Revision- and generation-guarded UUID inventory (archive schema 3)

`archive_inventory` adds required `sample_type`, `start`, `end`, `limit`, and
`cursor` to the standard authenticated HAL envelope. Dates must use UTC `Z`,
`start < end`, and `limit` must be an integer from 1 through 200. The first
request uses `cursor: null`. Only sample UUIDs are returned, with standard
`ok`, `request_type`, `protocol_version`, echoed `request_id`, `sample_ids`,
nonnegative integer `revision`, `owner_generation`, and nullable `next_cursor`.
Only current-generation originals are listed; older originals remain in admin
browse/export. See
[`archive-inventory-v2.json`](fixtures/archive-inventory-v2.json).

Membership is **sample start in `[start,end)`**, not interval overlap. A sample
starting before the lower bound is excluded even if it ends inside the range;
one starting inside is included even if it ends after the upper bound. Clients
must use this identical rule and must not infer deletions before the latest
HealthKit readable boundary. Rows sort by start then canonical lowercase UUID,
so same-time samples remain distinct. Tombstones are excluded.

Treat cursors as opaque strings (maximum 2,048 characters). They bind user,
type, both interval endpoints, page limit, revision, owner generation, and last key. Changing
query scope, malformed cursors, or invalid keys returns HTTP 422
`invalid_cursor`. Cursors are not credentials; each page needs the HAL token,
configured user binding, and current uploader proof. Inventory shares the
control rate budget.

The revision and rows are read within one SQLite snapshot. A subsequent page
with an outdated revision returns HTTP 409 `inventory_changed`; discard the
partial comparison and restart that interval from a null cursor.

A deletion-only `archive_batch` may include `expected_inventory_revision`, an
integer from 0 through 9,223,372,036,854,775,807. A new conditional batch checks
`expected_owner_generation` from the same inventory snapshot. Both fields are
required together on the wire. A transfer rejects the whole batch without a
receipt, tombstone, sample, or coverage change. The batch checks the current
user/type revision under the same `BEGIN IMMEDIATE` transaction as
its tombstones. Mismatch returns only `{"ok":false,"error":"inventory_changed"}`
with HTTP 409 and changes no samples, tombstones, coverage, projection jobs,
revision, or receipt. Ordinary batches retain their existing behavior. Every
new accepted batch increments that user/type revision once, including no-op
batches; an exact retry by the still-approved owner returns its original
receipt before checking the revision and does not increment it. A revoked owner
cannot retrieve that receipt. After an acknowledged conditional deletion,
restart inventory against the new revision before deleting another set of IDs.
An empty page has the current revision (zero for a never-imported scope).

Schema 1/2 upgrades atomically to schema 3, preserving originals and seeding durable per-user/type
revisions from accepted coverage rows. Revisions survive restart and backups.
Explicit archive deletion increments and retains revision rows to invalidate
outstanding comparisons across deletion and re-import. This metadata contains
only scope identifiers and counters. Inventory and conditional tombstones do
not assert HealthKit read permission or statistics readiness.

Capability responds with `ok`, `request_type`, `protocol_version`, echoed
`request_id`, `archive_schema_version`, batch limits, supported sample types and
metric keys, and separate `archive_available` and `statistics_available`
booleans. The installed fork must populate the lists from its audited mapping;
the fixture's three entries illustrate the shape and do not claim complete
support for all 111 live metric keys. Advertised limits may be lower than the
protocol ceilings of 262,144 bytes, 200 samples, and 200 deletions, but never
higher.

The packaged registry advertises 107 direct metrics from 99 source types:
101 statistics metrics and six timeline metrics. The integrated statistics
worker reports availability when recorder is ready and verifies readback before
status can report `current`. Acknowledgements report `pending` or `failed`;
they prove the raw archive commit, not recorder visibility. Projection expansion
is capped at 8,784 intersecting UTC hours per sample and 16,384 enumerated hours
per batch, including old correction/deletion intervals and repeated overlaps.
Exceeding a budget still archives all originals and tombstones atomically;
the receipt reports `failed`, and status reports `projection_range_exceeded`.
One durable repair intent replaces the affected type's jobs. Correct/delete
oversized originals and explicitly retry to rebuild surviving statistics.
Historical age is unrestricted. See [operations](../archive-operations.md).
The SQLite archive uses schema 3 and lives at
`.storage/health_bridge_archive.sqlite`, separately from recorder. Store opening,
commits, and status reads run in Home Assistant's executor. An unavailable store
does not prevent live integration setup; capability advertises
`archive_available: false`, and upload/status return HTTP 503.

Uploads are limited to 60 requests per minute per authenticated config entry;
capability/status share an independent 120-request budget so an import cannot
consume its status budget. HTTP 429 includes `Retry-After`. Invalid schemas and
unsupported types return 422, byte/count violations return 413, conflicting
reuse of a batch ID returns 409, and storage failures return 503 without a
commit acknowledgement. Rate windows are process-local and reset on restart.

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
| `owner_required` | No uploader is approved for this user; request approval. |
| `owner_pending` | This phone has a pending claim, or another phone has claimed the user. |
| `owner_changed` | The supplied phone is not the approved uploader. |

Authentication failure, unsupported advertised sample types, archive storage
failure, and projection failure are route/store concerns; they must not be
misreported as successful archive commits. Existing v1 error behavior is
unchanged.
