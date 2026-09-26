# Archive statistics rules and recovery

The archive advertises **107 directly readable app metrics across 99 unique
HealthKit types**: 96 quantity types, two category types, and one workout type.
All 111 live keys remain intact. The four unsupported keys are
`uv_exposure_sed` (no direct SED source), `net_calories` (derived),
`last_sync_time` and `test_connection` (server metadata).
No HealthKit source is fabricated for these keys.

The canonical packaged catalog is
[`archive_catalog_v2.json`](../custom_components/health_bridge/archive_catalog_v2.json).
The [protocol fixture](protocol/fixtures/archive-catalog-v2.json) is an exact,
tested copy for client implementation. Capability and projection rules both load
the packaged catalog. Each source records payload kind, canonical quantity unit,
minimum app OS, runtime availability condition, and its per-metric live
aggregation, hourly rule, display unit, conversion, source and interval policies.
The audit source is app `MetricRegistry`, `HealthObjectTypeID`, `UnitSymbol`
and `HealthKitTypeResolver` at `52f9a07f7af0373e521a1aebfeb60833c4f0ff25`.
All 19 v2.1.0 additions have direct quantity sources. Minimum iOS 18 is the
app registry floor; capability is not proof of device support, authorization or
readable history. Clients must resolve the type and query permitted originals.

| Rule family | Metrics | Interpretation |
| --- | ---: | --- |
| Quantity mean | 41 | sample-start arithmetic mean/min/max, all sources |
| Quantity total | 53 | elapsed-overlap prorating of original sample amounts, source-aware |
| Category duration | 7 | source-aware interval union: six sleep metrics in hours; mindful sessions in seconds |
| Timeline only | 6 | sleep details/start/wake, workouts, headphone/environmental audio exposure |

Quantity canonical-unit tokens are the app's `healthKitUnit` expressed as
`UnitSymbol.rawValue`, **not** the platform-dependent `HKUnit.unitString`.
Examples: `fraction` is a 0–1 HealthKit percentage; `rpm` and
`breaths/min` resolve to count/min; `MET` resolves to kcal/(kg·h);
`unitless` resolves to count for UV index. Originals retain their raw unit
separately. Statistics convert fractions to percent (×100) and daylight minutes
to seconds (×60). Exercise/stand minutes, nutrition g/mg/µg, distances m,
temperatures degC and other quantities retain their catalog units; temperature
metadata uses °C. Canonical micrograms use the app's micro-sign `µg`, while
HA mass metadata uses its Greek-mu spelling `μg`, with no numeric conversion.
Negative water temperatures down to the app's −10 °C lower
bound remain valid. Source units are explicit even for timeline-only audio.

Live aggregates are not hourly originals: `stand_time` is additive despite its
live measurement state class, sleep uses actual intervals rather than a daily
snapshot, and `mindful_minutes` produces seconds despite its key name. Latest
quantity readings become sample-start means, never forward-filled values.
Workout effort scores describe mean/min/max of recorded scores, not summed
effort. Audio dBA exposure remains timeline-only: arithmetic averaging of
logarithmic levels is not an energy-equivalent exposure.

The canonical workout type is `HKWorkoutType`, matching the app and HealthKit.
This corrects the pre-release fixture's invented `HKWorkoutTypeIdentifier`;
only the canonical name is negotiated. No installed archive migration is claimed
for the obsolete pre-release identifier. Live/backfill v1 are unaffected.

Sleep category 0 (in bed) is excluded; categories 1/3/4/5 contribute to total
sleep, and 2 to awake. Mindful session category 0 contributes its actual
duration. Unknown category values and invalid quantity units retain their
originals but fail numeric projection. Text categories and workouts remain
browseable without numeric statistics.

Quantity intervals distribute their recorded total by elapsed overlap, not
by copying a daily total into every hour. At overlapping intervals, the source
with the lexically first bundle ID wins that segment; other sources can fill
uncovered segments. Distinct UUIDs within the selected quantity source add.
At a zero-duration timestamp, competing point samples and intervals covering
that instant share the same source rule (interval ends are exclusive). This policy is
deterministic; it does not claim to reproduce Apple's private source priority.
Sleep durations union matching intervals from the selected source, excluding
in-bed category 0. Instantaneous means retain observations from all sources.
All boundaries are UTC and half-open; local time-zone provenance does not
change elapsed duration during daylight-saving transitions.

External IDs are `health_bridge:<metric>_<first-32-hex-of-SHA256-user-id>`.
They never reuse live `sensor.*` statistic IDs. Numeric metadata always includes
`unit_class`, display unit, `mean_type`, and `has_sum`. Additive `state` is the
hour's total; `sum` is the absolute cumulative total since the earliest archived
original. The hourly state does not imply a daily or lifetime live sensor state.

Ordinary means upsert only the affected hour. Additive corrections reconstruct
the cumulative baseline from archived originals and upsert the affected tail.
Source queries use 500-record pages; recorder imports contain at most 168 rows.
Recorder value/inventory reads cover at most 168 UTC hours per query, including
sparse gaps. Whole-series and tail absence use a latest-row query limited to one
result. A constant-size aggregate detects restored stale rows preceding the
earliest surviving original, including zero totals. These checks never
materialize every recorder hour through a maximum timestamp.
A sweep retains original samples only for the active hour, although an unusually
dense hour can still consume substantial memory. Pending additive jobs are
coalesced by a snapshot of job IDs. A concurrent correction replaces those IDs
and therefore cannot be falsely completed by an older replay.

The supported Home Assistant API cannot delete an individual external hour.
If an existing hour becomes empty, the worker first commits a full-rebuild job
at a reserved year-1 timestamp in the archive outbox. It then clears only that
user/type's integration-owned statistics and replays the surviving originals.
This exceptional repair temporarily removes that series from HA while it is
pending; raw archive records remain available. A restart or failure after clear
retains the repair job and replays it. Ordinary imports never clear whole series.

The worker uses HA `async_add_external_statistics`, recorder
`async_clear_statistics`, `statistics_during_period`, and `get_metadata`; it
does not write recorder SQL. Queue completion alone is insufficient: values,
timestamps, metadata, and deletion absence must be read back before completing
jobs. Transient failures retry after 30/60/120/240 seconds and stop after five
attempts with `failed`. Invalid data fails immediately. The authenticated archive
UI can call `ArchiveProjectionWorker.async_retry(user_id)` for explicit retry.
Before an archive status response reports `current`, it compares archived
projections and metadata with recorder again. This detects independently purged
or restored statistics, including missing older hours when newer ones survive.
A mismatch commits a replay-only sentinel job; it survives restart and upserts
missing/corrected hours without clearing surviving data. Existing empty-hour
repair still uses the distinct full-rebuild sentinel when deletion is necessary.
Status reconciliation scans relevant originals and bounded recorder windows, so
a multiyear status request may take time; it does not cache a stale current claim.
Archive receipts report `pending` or a durable projection `failed` state and prove only archive COMMIT. Statistics
completion comes from the separate reconciled status response.
Worker polling is 30 seconds; archive receipt acknowledgement never waits for it.
The worker starts with the health entry and stops when its last entry unloads;
Phone Assistant Link entries do not own it.

Test evidence uses the installed HA package and real recorder SQLite fixtures
for import, correction, deletion, migration and recovery. The pinned official
Home Assistant 2026.9.3 container gate covers installation, HAL/PAL/v1 behavior,
archive import, restart, backup restore, and purge/readback. iOS-device
end-to-end verification remains a separate release gate.

The currently qualified release minimum is **Home Assistant 2026.9.3 with
Python 3.14**. Earlier versions have not passed these archive/statistics gates;
API import fallback is not a claim of whole-fork compatibility. `hacs.json`
declares this qualified minimum. The catalog contains 107 direct metrics from
99 source types, with 101 statistics metrics and six timeline metrics.

Projection work has an explicit failure budget without discarding originals:
8,784 intersecting hours per sample and 16,384 enumerated hours per ingestion
batch, including old intervals and repeated overlaps. Oversized work commits
one failed full-rebuild intent with `projection_range_exceeded`. The worker
checks surviving interval sizes with constant memory before recorder mutation
and again while paging originals. Correct/delete the oversized source record,
then explicitly retry; see [operations](archive-operations.md). This uses the
existing schema-2 outbox and does not impose a historical-age limit.
