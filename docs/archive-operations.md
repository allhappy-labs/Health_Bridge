# Archive fork operations

This is the local `2.1.1a1` prerelease fork of upstream Health Bridge v2.1.0,
commit `399c6aa3c7af32d0d2faa49ae71286695532c0cb`. The qualified minimum is
**Home Assistant Core 2026.9.3 / Python 3.14.2**; verification uses Python 3.14.7.
Earlier HA versions have not been qualified for the recorder/statistics APIs.
HACS 2.0.0 or later is required for HACS installation. A newer HA release still
needs a compatibility check before upgrading; the minimum is not proof that
every future version works.

The integration domain remains `health_bridge`. Existing Health Assistant Link
(HAL) and Phone Assistant Link (PAL) entries, tokens, entity identities, all 111
live metric keys, v1 live acknowledgements, and v1 numeric/text backfill remain
compatible. V2 currently advertises 107 direct metrics from 99 source types;
four derived/internal keys have no direct source. See the packaged
[catalog](../custom_components/health_bridge/archive_catalog_v2.json).
V1 still has its 14-day recorder-only limit. Existing recorder rows are summaries
and **cannot be converted into original HealthKit samples**. V2 requires a
client implementing the archive protocol; upstream companion apps are not
claimed to support it. Full-history iOS qualification remains a separate gate.

## Install and upgrade from v2.1.0

1. Make a Home Assistant backup and download a protected copy. Save the exact
   previously installed integration version. Test the restore on a disposable
   instance before a large import. Preserve existing config entries and tokens.
2. Confirm HA is at least 2026.9.3. In HACS → ⋮ → Custom repositories, add
   `https://github.com/allhappy-labs/Health_Bridge` as an Integration. If the
   upstream `gregt1993/Health_Bridge` package is downloaded, remove it from
   HACS first. When HACS warns that the integration is configured, choose
   **Ignore** rather than deleting its Home Assistant configuration entry.
   Remove the upstream custom-repository registration if one exists. Only one
   package can own `custom_components/health_bridge`.
3. Download the `allhappy-labs` fork in HACS, checking the commit displayed in
   the confirmation dialog. Restart HA. Existing HAL/PAL entries should load
   without being recreated; verify version `2.1.1a1`, the fork documentation
   link, and their entities. Check the applicable HAL live and PAL ping routes,
   then query `archive_capability` over the
   existing `/api/webhook/health_bridge` route using the HAL token and intended
   phone's device-local uploader credential. Expect schema 3, ownership contract
   1, protocol 2, 107 supported metrics from 99 source types, and both
   archive/statistics availability true when recorder is ready. PAL credentials
   cannot upload an archive.
4. Add the `custom:health-bridge-archive` card using an administrator account;
   the integration registers its JavaScript resource automatically. Claim the
   user from the intended phone, compare its fingerprint with the pending
   claim in the admin card, and explicitly approve it. Before approving a
   replacement, back up HA; approval revokes the old phone's v2 access while
   retaining old-only originals. Browse a small synthetic or explicitly chosen
   import before expanding scope. Keep
   `samples archived` distinct from `statistics current`.

See [protocol requests](protocol/archive-v2.md), [archive card](archive-browser.md),
and [statistic semantics](archive-statistics.md).

### HACS prerelease boundary

The HACS 2.0.5 custom-repository switch above was exercised on a Home Assistant
Core 2026.9.3 target on 2026-10-01: HACS downloaded fork commit `2355078`, HA
restarted into version `2.1.1a1`, the existing HAL entry and 79 entities
remained, and several entity values were readable. The upstream HACS package
and custom-repository registration were removed. No fresh backup was created
for that target at the owner's direction. This is one installation observation,
not a verified HACS release, upgrade/rollback qualification, full-history phone
import, or backup/restore proof. Review future fork commits before downloading
them. The original upstream repository does **not** contain the archive feature.

For a manual fallback, stop HA, preserve the existing complete
`custom_components/health_bridge` directory for rollback, then replace it with
the complete directory from a reviewed fork commit. Do not overlay individual
Python files or copy the repository's virtual environment/tests. Ensure HACS
does not still track an upstream package that could replace these files.

## Storage, growth, and retention

Originals and recovery state live in
`<HA config>/.storage/health_bridge_archive.sqlite`, separate from
`home-assistant_v2.db`. SQLite may also create `-wal` and `-shm` sidecars. Archive
schema 3 includes originals, generation-scoped tombstones, idempotent receipts,
coverage intervals and authorization bounds, inventory revisions, owner digests,
pending claims, and pending/claimed/failed projection jobs. SQLite
transactions commit all of those before acknowledgement. Expired claimed jobs
become eligible again after their five-minute lease.

Recorder purge and `purge_keep_days` do not expire this archive. There is no
automatic archive retention limit. Growth depends on original sampling density,
metadata, import receipts, and correction/deletion history; dense multiyear
heart-rate data can be substantial. Inspect file sizes and free disk space
before and during large imports. Acknowledged rows survive restart; projection
may remain pending if recorder is unavailable. Long-term numeric statistics
live in recorder under the `health_bridge:` namespace; they are derived copies.

An original of any age or duration can be archived. Projection expansion is
limited to 8,784 intersecting UTC hours per sample and 16,384 enumerated hours
per batch (including old intervals on correction/deletion, and overlapping
intervals). Exceeding either budget commits the originals and receipt with
projection state `failed` and one durable full-rebuild intent; status reports
`projection_range_exceeded`. It never schedules the entire oversized range.
Correct or delete oversized originals at the source, sync those changes, then
use Retry in the archive card. Retry checks all surviving intervals before
clearing or writing recorder statistics. Batch-budget failures with individually
valid intervals can be retried directly. Originals remain browsable/exportable.
The failure intent uses the durable outbox and survives backup/restore.

Store and backup access exposes sensitive health records, source/device
provenance and authorization bounds. Protect the HA configuration directory,
backup encryption keys, exports and administrator credentials. HAL credentials
are integration-level authority; old entries without configured `user_id`
binding trust the authenticated client's claimed user ID. They are not a
per-person isolation boundary. Archive UI/API access is administrator-only.
The phone keeps its uploader credential in a non-synchronizing Keychain item;
HA stores only a digest. A replacement or lost Keychain credential needs a new
claim and explicit administrator approval. Transfer alone retains old originals.

## Backup

Use a Home Assistant backup **including Home Assistant configuration**. The
2026.9.3 Core backup engine includes `.storage/health_bridge_archive.sqlite`;
the recorder-database exclusion only targets `home-assistant_v2.db` and its WAL.
For full recovery, include recorder as well so derived statistics are restored.
The installed smoke test verifies a real backup contains a usable archive,
and the automated restore test covers all archive recovery tables.

The `health_bridge.backup` pre-backup hook drains archive operations, executes
`PRAGMA wal_checkpoint(TRUNCATE)`, and temporarily rejects archive operations
while HA copies configuration. Uploads receive a retryable 503 rather than a
commit acknowledgement. The projection worker retries after backup. A busy or
failed checkpoint fails the backup; post-backup resumes work even if the backup
failed. Do not run external SQLite writers against the archive during backup:
the write fence applies to this integration's store instance.
The successfully opened store survives HAL entry unload/reload for the lifetime
of the HA process, so reconfiguring an entry cannot bypass an active backup fence.

For a manual filesystem backup, stop HA and any external archive access first.
Use SQLite's backup operation or run `PRAGMA wal_checkpoint(TRUNCATE)` and verify
the returned busy count is zero before copying the main file. Never copy just
the main database while HA is writing; recently acknowledged rows may still be
in the WAL. Do not delete WAL/SHM files to force a checkpoint. Include the whole
HA configuration when preserving entry identities and credentials.

## Restore and rollback

1. Stop HA and preserve a separate copy of its current configuration/archive.
2. Restore the matching configuration backup, including the archive and,
   preferably, recorder. Do not mix a restored main SQLite file with WAL/SHM
   files from a different generation. For a standalone archive restore, move
   the old main file and its sidecars aside together before installing the
   checkpointed replacement while HA is stopped.
3. Run SQLite `PRAGMA integrity_check` (expect `ok`), `PRAGMA foreign_key_check`
   (expect no rows), and `PRAGMA user_version` (expect `3` for this release).
   Install the matching fork code and restart HA. Verify raw sample UUIDs,
   repeated batch receipts, uploader generation, coverage, and projection status. Pending work
   resumes; claimed jobs wait at most their remaining five-minute lease.
4. A backup predates later imports. Reconcile/rescan missing intervals from the
   source client; do not assume the client's newer local checkpoint means the
   restored server still has those samples. Query status after recorder loss;
   reconciliation requeues missing statistics, then confirm actual readback.

To roll back to upstream v2.1.0, stop HA and restore that complete integration
directory. Upstream cannot read or serve the archive, but does not migrate it
into recorder. Preserve the archive backup for reinstalling the fork. HAL/PAL
v1 continue to work; v2 imports must be disabled when capability is absent.
Newer unknown archive schema versions are rejected without modification;
downgrading code is not a schema downgrade. Restore a compatible backup instead.

## Export and explicit deletion

Use the archive card's bounded range query and NDJSON export while signed in
as an HA administrator. The authenticated API is
`GET /api/health_bridge/archive/{user_id}/export` with `sample_type`, `start`,
and `end` UTC parameters. It streams a manifest, originals and tombstones, then
a completion footer; tombstones have no original dates and are scoped to user/type.
Exports are plaintext sensitive data. An NDJSON export is an inspection/export
format, **not** a complete restorable SQLite backup (it lacks receipts, coverage
and queued jobs). Require the completion footer and pause imports/corrections
when producing a consistent export; pagination is not a database snapshot.

The card requires explicit user-scoped confirmation before
`POST /api/health_bridge/archive/{user_id}/delete` with
`{"confirm_user_id":"chosen-user","confirm":"DELETE"}`. It deletes that user's
archive originals, tombstones, receipts, coverage and queued projection work.
It does not erase recorder history/statistics, device/entity registrations,
Apple Health data, backups or downloaded exports. Delete those copies through
their own explicit workflows if required. Uninstalling the integration or
revoking HealthKit permission does not silently erase existing archived data.
Later resync can reimport originals after explicit archive deletion.

## Reproduce qualification

Use Python 3.14.2+ and the pinned dependencies in `pyproject.toml`. From the fork:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q
.venv/bin/ruff check custom_components tests
node --test tests/test_archive_card.cjs
python3 -m json.tool hacs.json
python3 -m json.tool custom_components/health_bridge/manifest.json
git grep -nE '(token|secret|health_value)=' -- '*.log'
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/installed_smoke.py
```

The log scan should exit 1 with no matches. The installed smoke test always
creates a new `.installed-verification/run-*` configuration, binds only to
loopback, installs the local component, creates disposable HAL/PAL entries and
an administrator, claims and approves two synthetic phone credentials in turn,
verifies wrong/revoked phone rejection, retains an old-only original, reuploads
and deletes a shared UUID under the new generation, submits synthetic 2024
fixtures, backs up, restores the archive into the stopped disposable config,
restarts HA, purges only that disposable recorder, and reads raw originals and hourly
statistics back through authenticated HTTP/WebSocket APIs. It records HA
version, fork SHA, working-tree dirtiness, schema, and observations in
`verification.json`. Never point a diagnostic purge at a real installation.
The verifier waits for both entries to report loaded, checks each owner
transition, and requires normal process
exit. The local macOS/Homebrew Python 3.14.7 environment currently exposes a
native interpreter-finalization crash on HA shutdown; successful functional
checks before shutdown do not satisfy that shutdown gate. Forced cleanup kills
only the verifier's owned child, waits for termination, and records the result.

This establishes the local Core installation path, not HA OS/Supervisor restore,
public HACS delivery, physical iOS 27 import, interruption/resume on a phone, or
permission-expansion behavior. Those remain distinct release gates.
