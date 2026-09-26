# Original-sample archive browser

Add this dashboard card after installing the fork (its resource registers automatically):

```yaml
type: custom:health-bridge-archive
user_id: person-1
```

Use the exact Health Bridge person ID sent by the app. All data operations require
a Home Assistant administrator. There is no verified mapping from HA accounts to
Health Bridge people; ordinary authenticated HA users cannot browse any person's
archive by guessing IDs. A future non-administrator feature needs an explicit
pairing/authorization model. The public card JavaScript contains no archive data.

Choose a sample type and an explicit UTC interval. The start is inclusive, the end
exclusive; intervals are limited to 36,600 days. Each page contains up to 200
originals (API maximum 500), ordered by start and UUID. Intervals that overlap the
range are included. Identical timestamps do not merge distinct originals. Select
a sample to inspect original units, source, metadata, workout details or sleep
stages. No readable samples does not diagnose HealthKit permission.

Projection state is separate from archive availability. `pending` and `failed`
do not claim statistics visibility. `current` comes from recorder readback; if
recorder is unavailable it is downgraded to pending. Timeline-only metrics have no
numeric statistics obligation. Retry requeues failed jobs for the chosen person.
The multiyear trend opens HA's statistics graph from the chosen start through
today (its end is not the sample filter's end). Links also open statistics settings
and the associated live sensor history when its entity exists. Status reconciliation
may take time for large archives.

## Export and access

The card streams exports directly to a file using the browser's File System
Access API. This currently needs a supporting secure-context desktop browser
such as Chrome/Edge; unsupported browsers display a clear limitation. The API
is independently available to any authenticated HA administrator client. Neither
server nor card buffers the entire archive. The browser aborts the destination
if the completion footer is absent, including a server error or truncation.

Routes under `/api/health_bridge/archive/{user_id}/`:

| Method / suffix | Parameters / body |
| --- | --- |
| GET `samples` | `sample_type`, UTC `start`, UTC `end`, optional `limit` (1–500), `cursor` |
| GET `sample` | `sample_type`, `uuid` |
| GET `export` | Same range parameters, optional page size; no cursor |
| GET `status` | None |
| POST `retry` | Empty JSON object |
| POST `delete` | Exactly `{"confirm_user_id":"person-1","confirm":"DELETE"}` for that URL person |

Use Home Assistant bearer authentication, never the app webhook token or a token
in the URL. Responses use `Cache-Control: no-store`. Errors expose stable codes,
not values, credentials or database details. Cursor scope includes person, type
and range; changing any invalidates it. Pagination is not a database snapshot:
pause importing/correcting while producing a consistent export.

The version-1 JSON Lines export begins with a manifest specifying person, type,
range, `snapshot:false`, and `tombstone_scope:all_for_user_and_type`. Each sample
line contains its original schema-versioned payload. Tombstone lines contain UUID
and originating batch ID. **Tombstones have no original timestamp, so all tombstones
for the chosen person/type are included regardless of the sample range.** The
last line must have `kind:complete` and sample/tombstone counts. A missing footer
or `kind:error` means an incomplete export. Protect exported health data.

## Archive-only deletion

Pause imports first. Type the exact person ID and accept the explicit browser
confirmation. The server separately verifies administrator access and exact
user-scoped confirmation. Deletion removes that person's originals, tombstones,
receipts, coverage and projection work only; other people remain untouched.
An already-claimed projection is invalidated so it cannot clear retained statistics
after deletion. Future app uploads can add records again.

**Recorder history, external statistics, backups, and prior exports are NOT
deleted. This control is not a privacy wipe.** These separate copies require
their own deliberate retention/deletion procedures. Permission revocation alone
also does not delete originals already committed in Home Assistant.

Verification here is automated HTTP/SQLite/recorder and JavaScript stream testing.
A real dashboard browser run and installed Home Assistant/iOS run remain release
gates; source/resource and stream tests do not establish rendered layout or live
device behavior.
