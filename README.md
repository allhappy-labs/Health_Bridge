# Health Bridge archive fork

This is an independent [Health Bridge](https://github.com/gregt1993/Health_Bridge) fork based on upstream v2.1.0. It preserves the existing Health Assistant Link (Apple Health) and Phone Assistant Link (Screen Time) entries and protocol-1 live/backfill behavior, and adds protocol-2 durable HealthKit original-sample archiving for the companion [HA Health Sync iOS app](https://github.com/allhappy-labs/HAHealthSync). It is not maintained by the upstream author or affiliated with the upstream iOS apps.

The archive stores authorized original quantity, category and workout samples separately from Home Assistant's recorder, including sample IDs, time ranges and provenance. Numeric metrics can also project hourly long-term statistics. The fork advertises 107 direct archive metrics from 99 HealthKit types; actual availability depends on the iPhone and Health permissions. Protocol-1 import remains limited to the latest 14 days and does **not** become a durable original-sample archive.

## Requirements and status

- Home Assistant Core **2026.9.3 or newer** with Python **3.14.2 or newer**. Those are the qualified minimums, not a guarantee of compatibility with every newer release.
- The [HA Health Sync iOS app](https://github.com/allhappy-labs/HAHealthSync) on iOS 27 for full-history import. Upstream companion apps are not claimed to implement protocol 2.
- This `2.1.1a3` fork is a source prerelease. Disposable Home Assistant Core tests passed, but a physical iOS 27 import and target Home Assistant backup/restore are not yet verified. Do not rely on it as the only copy of health history.

## Install or upgrade

1. Make and download a Home Assistant backup. Record the existing Health Bridge version, and test restore on a disposable instance before a large import.
2. In HACS → ⋮ → **Custom repositories**, add `https://github.com/allhappy-labs/Health_Bridge` as an **Integration**.
3. If HACS already downloaded `gregt1993/Health_Bridge`, remove that downloaded package first. HACS may warn that Health Bridge is configured: choose **Ignore** to keep the existing Home Assistant configuration entry; do **not** navigate to delete it. Remove the upstream custom-repository registration too, if present, so HACS tracks only this fork.
4. Open the `allhappy-labs` Health Bridge entry in HACS and choose **Download**. Check the displayed commit before confirming. Restart Home Assistant; do not add a second Health Bridge configuration entry or replace existing tokens.
5. Confirm the installed integration is version `2.1.1a3`, its documentation points to `allhappy-labs`, and existing entries and entities load. In HA Health Sync, Historical Import → Compatibility should report **Archive protocol 2 available**. Request archive approval on the intended iPhone, then open Home Assistant **Settings → Devices & services → Health Bridge → Configure → Archive uploader approvals**. Select the pending phone, compare and type the fingerprint shown on the iPhone, and approve it. Import a small range first. The `custom:health-bridge-archive` dashboard card is optional for browsing and exporting originals; it is no longer needed for approval.

The [operations guide](docs/archive-operations.md) covers manual fallback, backup/restore, storage, export, deletion and rollback. Only one `health_bridge` integration can be installed. The HACS custom-repository switch was verified on Home Assistant Core 2026.9.3 with HACS 2.0.5 at fork commit `2355078`; release-tag upgrades, rollback through HACS, other installations, and phone-connected full-history import remain unverified. Review future fork updates before downloading them; do not reinstall the upstream repository over this fork.

## Data and privacy

The archive persists in `<HA config>/.storage/health_bridge_archive.sqlite` and is not purged with ordinary recorder history. Home Assistant administrators and configuration backups can access it. Revoking iPhone Health permissions or uninstalling the app does not delete archived data. Archive deletion is a separate administrator action; recorder statistics and backups require their own retention/deletion handling. Protect server access, backups and exports. One admin-approved iPhone is authoritative per Health Bridge user; transferring ownership revokes the old phone's archive writes but preserves its old-only originals.

## Development

The integration has Python tests, Ruff checks, Node card tests and a disposable installed-Home-Assistant smoke test. See [archive operations](docs/archive-operations.md#reproduce-qualification) for the exact commands and their limits. The wire contract and fixtures are in [archive-v2](docs/protocol/archive-v2.md). Do not point diagnostic purge or smoke tests at a real Home Assistant configuration.

## License and attribution

MIT licensed; see [LICENSE](LICENSE). Based on [gregt1993/Health_Bridge](https://github.com/gregt1993/Health_Bridge) v2.1.0, retaining its upstream history and copyright attribution. The archive implementation and this fork's documentation are by Oleh Vdovenko and contributors. The upstream integration and its two companion apps remain separate projects.
