"""Opt-in installed smoke test; always creates its own loopback-only HA config.

Run: PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/installed_smoke.py
Container: PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/installed_smoke.py --runtime container --expected-sha COMMITTED_SHA
No production URL/config can be supplied. Only synthetic health data is used.
"""

import asyncio
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

import aiohttp


ROOT = Path(__file__).resolve().parents[1]
HAL = "installed-smoke-hal-synthetic-token-00001"
PAL = "installed-smoke-pal-synthetic-token-00001"
PHONE_A = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
PHONE_B = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"
TYPE = "HKQuantityTypeIdentifierStepCount"
USER = "person-1"
SAMPLE_A = "bd085ccc-22f4-4e80-a865-149bb5b0d1d4"
SAMPLE_B = "bd085ccc-22f4-4e80-a865-149bb5b0d1d5"
SID = "health_bridge:steps_" + hashlib.sha256(USER.encode()).hexdigest()[:32]
CONTAINER_IMAGE = "ghcr.io/home-assistant/home-assistant@sha256:d8922685169707fd91e8b9729902d975f06157d005e422874d201e0261dda196"
CONTAINER_HA_VERSION = "2026.9.3"


async def stop_owned_process(process, *, timeout=40):
    """Reap our child after SIGTERM, escalating only that child on timeout."""
    if process.poll() is not None:
        return False
    process.send_signal(signal.SIGTERM)
    try:
        await asyncio.to_thread(process.wait, timeout)
        return False
    except subprocess.TimeoutExpired:
        process.kill()
        await asyncio.to_thread(process.wait)
        return True


def container_command(config, port, name):
    """Launch only the pinned image with this disposable config and loopback port."""
    return [
        "docker", "run", "--rm", "--pull=never", "--name", name,
        "--network", "bridge", "--publish", f"127.0.0.1:{port}:8123",
        "--volume", f"{config}:/config:rw",
        "--env", "PYTHONDONTWRITEBYTECODE=1", CONTAINER_IMAGE,
    ]


def recorder_old_states(recorder, cutoff, container_name):
    """Read a live recorder database from the same filesystem as its writer."""
    if container_name:
        script = (
            "import sqlite3,sys; "
            "db=sqlite3.connect('file:/config/home-assistant_v2.db?mode=ro', uri=True); "
            "print(db.execute('SELECT count(*) FROM states WHERE last_updated_ts < ?', "
            "(float(sys.argv[1]),)).fetchone()[0])"
        )
        output = subprocess.check_output(
            ["docker", "exec", container_name, "python3", "-c", script, str(cutoff)],
            text=True, timeout=15,
        )
        return int(output.strip())
    with sqlite3.connect(recorder) as db:
        return db.execute("SELECT count(*) FROM states WHERE last_updated_ts < ?", (cutoff,)).fetchone()[0]


async def stop_owned_container(process, name, *, timeout=40):
    """Stop the exact container, then reap its attached Docker client."""
    if process.poll() is not None:
        return False
    try:
        stopped = await asyncio.to_thread(
            subprocess.run, ["docker", "stop", "--time", str(timeout), name],
            capture_output=True, text=True, timeout=timeout + 10,
        )
        if stopped.returncode != 0:
            raise RuntimeError(f"docker stop failed for {name}: {stopped.stderr.strip()}")
        await asyncio.to_thread(process.wait, timeout + 10)
        return False
    except (subprocess.TimeoutExpired, RuntimeError):
        removed = await asyncio.to_thread(
            subprocess.run, ["docker", "rm", "-f", name],
            capture_output=True, text=True, timeout=15,
        )
        if removed.returncode != 0:
            raise RuntimeError(f"docker rm -f failed for {name}: {removed.stderr.strip()}")
        await asyncio.to_thread(process.wait, 15)
        return True


def read_core_exit_code(log_path, start_offset):
    """Read the one HA Core exit recorded for this container run's log segment."""
    segment = log_path.read_bytes()[start_offset:].decode("utf-8", errors="replace")
    codes = re.findall(r"Home Assistant Core finish process exit code (-?\d+)", segment)
    return int(codes[0]) if len(codes) == 1 else None


async def record_owned_shutdown(process, container_name, log_path, start_offset):
    """Record an owned child even if it exited before shutdown was requested."""
    if process.poll() is None:
        forced = (await stop_owned_container(process, container_name) if container_name
                  else await stop_owned_process(process))
    else:
        await asyncio.to_thread(process.wait)
        forced = False
    record = {
        "pid": process.pid, "exit_code_source": "docker_run" if container_name else "owned_child",
        "forced_kill": forced, "returncode": process.returncode,
    }
    if container_name:
        record["core_returncode"] = read_core_exit_code(log_path, start_offset)
    return record


def shutdown_clean(record, runtime):
    return (record["returncode"] == 0 and not record["forced_kill"]
            and (runtime != "container" or record.get("core_returncode") == 0))


def shutdowns_clean(shutdowns, runtime):
    return len(shutdowns) == 2 and all(shutdown_clean(item, runtime) for item in shutdowns)


def assert_expected_source(report, runtime, expected_sha):
    if runtime == "container":
        assert expected_sha, "Container verification requires --expected-sha"
        assert report["fork_sha"] == expected_sha, (report["fork_sha"], expected_sha)
        assert not report["working_tree_dirty"], "Fork checkout is dirty"


OWNER_CHECKS = (
    "unbound_rejected", "first_admin_approval", "wrong_phone_rejected",
    "transfer_admin_approval", "revoked_phone_rejected",
    "old_only_original_preserved", "same_uuid_reuploaded_and_deleted",
    "exact_receipt_retry", "backup_restored_owner_and_archive",
)


def assert_owner_checks(report):
    for name in OWNER_CHECKS:
        assert report["checks"].get(name) is True, name


async def main(runtime="local", expected_sha=None):
    runs = ROOT / ".installed-verification"
    runs.mkdir(exist_ok=True)
    config = Path(tempfile.mkdtemp(prefix="run-", dir=runs))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    shutil.copytree(ROOT / "custom_components/health_bridge", config / "custom_components/health_bridge")
    server_host = "0.0.0.0" if runtime == "container" else "127.0.0.1"
    server_port = 8123 if runtime == "container" else port
    (config / "configuration.yaml").write_text(f"""
homeassistant:
  name: Archive disposable verification
  latitude: 0
  longitude: 0
  elevation: 0
  unit_system: metric
  time_zone: UTC
http:
  server_host: {server_host}
  server_port: {server_port}
frontend:
api:
config:
websocket_api:
lovelace:
backup:
recorder:
  commit_interval: 1
  auto_purge: false
  purge_keep_days: 1
""")
    container_name = f"health-bridge-smoke-{secrets.token_hex(6)}" if runtime == "container" else None
    report = {"config": str(config), "runtime": runtime, "fork_sha": subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "working_tree_dirty": bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=ROOT, text=True)), "checks": {}}
    if container_name:
        report.update({"container_name": container_name, "container_image": CONTAINER_IMAGE})
    assert_expected_source(report, runtime, expected_sha)
    print(f"Disposable config: {config}", flush=True)
    process = None
    log_path = config / "process.log"
    log = log_path.open("a")
    access = None
    start_log_offset = 0
    recorded_processes = set()

    def start():
        nonlocal start_log_offset
        log.flush()
        start_log_offset = log_path.stat().st_size
        if container_name:
            return subprocess.Popen(container_command(config, port, container_name),
                                    cwd=config, stdout=log, stderr=subprocess.STDOUT)
        return subprocess.Popen(
            [sys.executable, "-m", "homeassistant", "--config", str(config), "--skip-pip"],
            cwd=config, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONFAULTHANDLER": "1"},
            stdout=log, stderr=subprocess.STDOUT,
        )

    async def stop():
        if process and process not in recorded_processes:
            shutdown = await record_owned_shutdown(process, container_name, log_path, start_log_offset)
            report.setdefault("shutdowns", []).append(shutdown)
            recorded_processes.add(process)
            if shutdown["forced_kill"]:
                print(f"Shutdown timeout: killed and reaped owned HA child {process.pid}", flush=True)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async def request(method, path, *, auth=True, **kwargs):
            headers = {"Authorization": f"Bearer {access}"} if auth and access else {}
            async with session.request(method, base + path, headers=headers, **kwargs) as response:
                assert response.status < 400, (path, response.status, await response.text())
                return await response.json()

        async def response(method, path, *, auth=True, **kwargs):
            headers = {"Authorization": f"Bearer {access}"} if auth and access else {}
            async with session.request(method, base + path, headers=headers, **kwargs) as result:
                return result.status, await result.json()

        async def wait_ready():
            for _ in range(120):
                assert process.poll() is None, f"HA exited {process.returncode}; inspect {config}/process.log"
                try:
                    async with session.get(base + "/api/onboarding") as response:
                        if response.status in (200, 401):
                            return
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(1)
            raise AssertionError("HA startup timed out")

        async def webhook_response(payload, token=HAL):
            return await response("POST", "/api/webhook/health_bridge", auth=False,
                                  json={"token": token, "user_id": USER, **payload})

        async def webhook(payload, token=HAL):
            status, body = await webhook_response(payload, token)
            assert status < 400, (payload["request_type"], status, body)
            return body

        def control(kind, credential, request_id):
            return {"request_type": kind, "protocol_version": 2,
                    "request_id": request_id, "uploader_credential": credential}

        async def approve(claim, generation):
            owner = await request("GET", f"/api/health_bridge/archive/{USER}/owner")
            assert owner["pending_claim"]["claim_id"] == claim["claim_id"]
            assert owner["pending_claim"]["fingerprint"] == claim["fingerprint"]
            approved = await request("POST", f"/api/health_bridge/archive/{USER}/owner-approve",
                                     json={"claim_id": claim["claim_id"],
                                           "confirm_user_id": USER, "confirm": "APPROVE"})
            assert approved["owner_generation"] == generation, approved
            return approved

        async def wait_entries_loaded():
            for _ in range(120):
                assert process.poll() is None, f"HA exited {process.returncode}"
                entries = await request("GET", "/api/config/config_entries/entry",
                                        params={"domain": "health_bridge"})
                if len(entries) == 2 and all(entry["state"] == "loaded" for entry in entries):
                    return
                await asyncio.sleep(1)
            raise AssertionError("Health Bridge entries did not load")

        async def wait_current(credential=PHONE_A):
            for _ in range(90):
                status = await webhook(control("archive_status", credential, "installed-status"))
                metrics = status["metrics"]
                if metrics and all(item["state"] == "current" for item in metrics):
                    return status
                await asyncio.sleep(1)
            raise AssertionError(f"Statistics not current: {status}")

        async def statistics(expected_sum):
            async with session.ws_connect(base + "/api/websocket") as ws:
                assert (await ws.receive_json())["type"] == "auth_required"
                await ws.send_json({"type": "auth", "access_token": access})
                assert (await ws.receive_json())["type"] == "auth_ok"
                await ws.send_json({"id": 1, "type": "recorder/statistics_during_period",
                                    "start_time": "2024-01-01T00:00:00Z", "end_time": "2024-01-02T00:00:00Z",
                                    "period": "hour", "statistic_ids": [SID], "types": ["sum", "state"]})
                result = await ws.receive_json()
                assert result["success"], result
                rows = result["result"][SID]
                assert len(rows) == 1 and rows[0]["sum"] == expected_sum, rows
                return rows

        async def raw(expected):
            result = await request("GET", f"/api/health_bridge/archive/{USER}/samples", params={
                "sample_type": TYPE, "start": "2024-01-01T00:00:00Z", "end": "2024-01-02T00:00:00Z"})
            assert {item["uuid"]: item["payload"]["raw_value"]
                    for item in result["samples"]} == expected, result
            return result

        try:
            process = start()
            await wait_ready()
            code = await request("POST", "/api/onboarding/users", auth=False, json={
                "name": "Disposable owner", "username": "disposable", "password": secrets.token_urlsafe(32),
                "client_id": base + "/", "language": "en"})
            credentials = await request("POST", "/auth/token", auth=False, data={
                "grant_type": "authorization_code", "code": code["auth_code"], "client_id": base + "/"})
            access = credentials["access_token"]
            report["homeassistant_version"] = (await request("GET", "/api/config"))["version"]
            if runtime == "container":
                assert report["homeassistant_version"] == CONTAINER_HA_VERSION, report["homeassistant_version"]
            for app, token in (("health_assistant_link", HAL), ("phone_assistant_link", PAL)):
                flow = await request("POST", "/api/config/config_entries/flow", json={"handler": "health_bridge", "show_advanced_options": False})
                path = f"/api/config/config_entries/flow/{flow['flow_id']}"
                await request("POST", path, json={"next_step_id": app})
                entry = await request("POST", path, json={"token": token})
                assert entry["type"] == "create_entry", entry
            await wait_entries_loaded()
            report["checks"]["both_entries"] = True
            assert (await webhook({"request_type": "phone_assistant_link", "client": "phone_assistant_link", "action": "ping"}, PAL))["protocol_version"] == 3
            now = datetime.now(timezone.utc)
            live = await webhook({"request_type": "live", "protocol_version": 1, "request_id": "installed-live", "data": {"steps": [{"timestamp": now.isoformat(), "value": 42}]}})
            assert live["applied"] is True
            report["integration_version"] = live["integration_version"]
            backfill = await webhook({"request_type": "backfill", "protocol_version": 1, "request_id": "installed-backfill", "data": {"steps": [{"timestamp": (now - timedelta(days=2)).isoformat(), "value": 7}, {"timestamp": now.isoformat(), "value": 42}]}})
            assert backfill["committed"] is True, backfill
            report["checks"]["v1_live_backfill_pal"] = True
            fixture = json.loads((ROOT / "docs/protocol/fixtures/archive-batch-v2.json").read_text())
            fixture["uploader_credential"] = PHONE_A
            fixture["samples"].append({**fixture["samples"][0], "uuid": SAMPLE_B,
                                       "start": "2024-01-01T10:30:00Z",
                                       "end": "2024-01-01T10:30:01Z",
                                       "payload": {**fixture["samples"][0]["payload"],
                                                   "raw_value": 5, "canonical_value": 5}})
            denied_status, denied = await webhook_response(fixture)
            assert denied_status == 403 and denied["error"] == "owner_required", denied
            report["checks"]["unbound_rejected"] = True
            first_claim = await webhook(control("archive_owner_claim", PHONE_A, "claim-a"))
            assert first_claim["owner_state"] == "pending" and first_claim["owner_generation"] == 0
            await approve(first_claim, 1)
            report["checks"]["first_admin_approval"] = True
            capability = await webhook(control("archive_capability", PHONE_A, "installed-capability"))
            assert capability["archive_available"] and capability["statistics_available"]
            assert len(capability["supported_metrics"]) == 107
            assert capability["archive_schema_version"] == 3
            assert capability["ownership_contract_version"] == 1
            assert capability["owner_state"] == "active" and capability["owner_generation"] == 1
            report["capability"] = capability
            ack = await webhook(fixture)
            assert ack["committed_samples"] == 2
            assert await webhook(fixture) == ack
            report["checks"]["exact_receipt_retry"] = True
            report["archive_receipt"] = ack
            report["statistics_before_restart"] = await wait_current()
            report["raw_before_restart"] = await raw({SAMPLE_A: 12, SAMPLE_B: 5})
            report["statistics_readback_before_restart"] = await statistics(17)
            wrong_status, wrong = await webhook_response(control("archive_status", PHONE_B, "wrong-status"))
            assert wrong_status == 403 and wrong["error"] == "owner_changed", wrong
            report["checks"]["wrong_phone_rejected"] = True
            second_claim = await webhook(control("archive_owner_claim", PHONE_B, "claim-b"))
            assert second_claim["owner_state"] == "pending" and second_claim["owner_generation"] == 1
            await approve(second_claim, 2)
            report["checks"]["transfer_admin_approval"] = True
            revoked_status, revoked = await webhook_response(fixture)
            assert revoked_status == 403 and revoked["error"] == "owner_changed", revoked
            report["checks"]["revoked_phone_rejected"] = True
            inventory = {**control("archive_inventory", PHONE_B, "inventory-b"),
                         "sample_type": TYPE, "start": "2024-01-01T00:00:00Z",
                         "end": "2024-01-02T00:00:00Z", "limit": 200, "cursor": None}
            page = await webhook(inventory)
            assert page["sample_ids"] == [] and page["owner_generation"] == 2, page
            deletion = {**fixture, "uploader_credential": PHONE_B,
                        "request_id": "delete-old-a", "batch_id": "delete-old-a",
                        "samples": [], "deletions": [SAMPLE_A]}
            await webhook(deletion)
            await raw({SAMPLE_A: 12, SAMPLE_B: 5})
            await statistics(17)
            report["checks"]["old_only_original_preserved"] = True
            reupload = {**fixture, "uploader_credential": PHONE_B,
                        "request_id": "reupload-a", "batch_id": "reupload-a",
                        "samples": [fixture["samples"][0]], "deletions": []}
            reupload_ack = await webhook(reupload)
            assert reupload_ack["committed_samples"] == 1
            assert await webhook(reupload) == reupload_ack
            current_page = await webhook({**inventory, "request_id": "inventory-current"})
            assert current_page["sample_ids"] == [SAMPLE_A], current_page
            final_delete = {**deletion, "request_id": "delete-current-a",
                            "batch_id": "delete-current-a"}
            delete_ack = await webhook(final_delete)
            assert delete_ack["committed_deletions"] == 1
            assert await webhook(final_delete) == delete_ack
            await wait_current(PHONE_B)
            await raw({SAMPLE_B: 5})
            await statistics(5)
            report["checks"]["same_uuid_reuploaded_and_deleted"] = True
            await request("POST", "/api/services/backup/create", json={})
            for _ in range(60):
                backups = list((config / "backups").glob("*.tar"))
                if backups:
                    break
                await asyncio.sleep(1)
            assert backups
            # Confirm the real HA backup contains a stand-alone, usable archive.
            await asyncio.sleep(2)
            with tarfile.open(backups[0]) as outer:
                member = next(item for item in outer.getmembers() if item.name.endswith("homeassistant.tar.gz"))
                with tarfile.open(fileobj=io.BytesIO(outer.extractfile(member).read())) as inner:
                    db_member = next(item for item in inner.getmembers() if item.name.endswith(".storage/health_bridge_archive.sqlite"))
                    restored = config / "backup-restored.sqlite"
                    restored.write_bytes(inner.extractfile(db_member).read())
            with sqlite3.connect(restored) as db:
                assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
                assert db.execute("SELECT count(*) FROM samples").fetchone() == (1,)
                assert db.execute("SELECT owner_generation FROM samples").fetchone() == (1,)
                assert db.execute("SELECT generation FROM archive_owners").fetchone() == (2,)
                report["archive_schema"] = db.execute("PRAGMA user_version").fetchone()[0]
                assert report["archive_schema"] == 3
            report["checks"]["ha_backup_contains_restorable_archive"] = True
            await stop()
            assert shutdown_clean(report["shutdowns"][-1], runtime), report["shutdowns"][-1]
            # Restore the checkpointed archive from HA's backup into only this
            # stopped disposable configuration. Keep its prior files for audit.
            archive_path = config / ".storage/health_bridge_archive.sqlite"
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(str(archive_path) + suffix)
                if candidate.exists():
                    candidate.rename(config / ("pre-restore-archive.sqlite" + suffix))
            shutil.copyfile(restored, archive_path)
            process = start()
            await wait_ready()
            await wait_entries_loaded()
            retry = await webhook(final_delete)
            assert retry == delete_ack
            report["checks"]["restart_receipt_idempotence"] = True
            restored_capability = await webhook(control("archive_capability", PHONE_B, "restored-capability"))
            assert restored_capability["owner_state"] == "active"
            assert restored_capability["owner_generation"] == 2
            restored_status, restored_error = await webhook_response(control("archive_status", PHONE_A, "restored-old"))
            assert restored_status == 403 and restored_error["error"] == "owner_changed"
            report["checks"]["backup_restored_owner_and_archive"] = True
            await wait_current(PHONE_B)
            report["raw_after_restart"] = await raw({SAMPLE_B: 5})
            recorder = config / "home-assistant_v2.db"
            cutoff = (now - timedelta(days=1)).timestamp()

            def old_states():
                return recorder_old_states(recorder, cutoff, container_name)

            assert old_states() >= 1, "v1 backfill did not create purgeable recorder rows"
            await request("POST", "/api/services/recorder/purge", json={"keep_days": 1, "repack": False})
            for _ in range(60):
                if old_states() == 0:
                    break
                await asyncio.sleep(1)
            assert old_states() == 0
            report["checks"]["recorder_old_rows_purged"] = True
            report["raw_after_purge"] = await raw({SAMPLE_B: 5})
            report["statistics_readback_after_purge"] = await statistics(5)
            report["checks"]["post_purge_raw_and_statistics"] = True
            assert_owner_checks(report)
            print(json.dumps({key: value for key, value in report.items() if key in {"fork_sha", "working_tree_dirty", "homeassistant_version", "integration_version", "archive_schema", "checks"}}, indent=2), flush=True)
        finally:
            await stop()
            log.close()
            report["checks"]["clean_shutdowns"] = shutdowns_clean(report.get("shutdowns", []), runtime)
            report["finished_at"] = time.time()
            (config / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
        assert report["checks"]["clean_shutdowns"], report.get("shutdowns", [])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("local", "container"), default="local")
    parser.add_argument("--expected-sha")
    args = parser.parse_args()
    asyncio.run(main(args.runtime, args.expected_sha))
