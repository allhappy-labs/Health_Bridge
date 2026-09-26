# Archive statistics rules and recovery

The archive negotiates four original HealthKit types. This is a subset of the
111-key live registry, not support for importing every live metric's originals.

| Original type | Negotiated metrics | Rule |
| --- | --- | --- |
| `HKQuantityTypeIdentifierStepCount` | `steps` | canonical `count`; prorated source-aware hourly totals |
| `HKQuantityTypeIdentifierHeartRate` | `heart_rate` | canonical `count/min`; sample-start arithmetic mean/min/max |
| `HKCategoryTypeIdentifierSleepAnalysis` | `sleep_duration`, `sleep_rem_hours`, `sleep_core_hours`, `sleep_deep_hours`, `sleep_awake_hours`, `sleep_unspecified_hours` | actual overlap in UTC hours, duration union, cumulative hours |
| `HKCategoryTypeIdentifierSleepAnalysis` | `sleep_details`, `asleep_time`, `wake_time` | original timeline only; no numeric statistics |
| `HKWorkoutTypeIdentifier` | `last_apple_workout` | original timeline only; no numeric statistics |

Other live keys are not advertised by archive v2. Adding them requires an
explicit HealthKit original type, canonical unit, source policy and projection
rule, with matching app mapping and fixtures. Live sensor `state_class` is not
used to infer archive semantics. Invalid units and unknown sleep categories
retain their originals and surface a projection failure.

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
Archive receipts report `pending` and prove only archive COMMIT. Statistics
completion comes from the separate reconciled status response.
Worker polling is 30 seconds; archive receipt acknowledgement never waits for it.
The worker starts with the health entry and stops when its last entry unloads;
Phone Assistant Link entries do not own it.

Test evidence uses the installed HA package and real recorder SQLite fixtures.
An installed Home Assistant deployment, recorder purge, backup restore, and
iOS-device end-to-end verification remain separate release gates.

The currently qualified release minimum is **Home Assistant 2026.9.3 with
Python 3.14**. Earlier versions have not passed these archive/statistics gates;
API import fallback is not a claim of whole-fork compatibility. Task 7 must
reconcile the inherited `hacs.json` minimum of 2024.12.0 with that precise
qualified baseline (or qualify an earlier exact version), and run installation,
import, correction, deletion, restart, purge/readback and upgrade checks against
the declared minimum before publication. This task does not change the manifest.
