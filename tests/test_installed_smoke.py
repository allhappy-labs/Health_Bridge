"""Owned-process cleanup must not strand a disposable HA process on timeout."""

import asyncio
import signal
import sqlite3
import subprocess
import sys

import installed_smoke
import pytest


async def test_shutdown_reaps_owned_child_that_ignores_sigterm():
    assert hasattr(installed_smoke, "stop_owned_process")
    child = subprocess.Popen(
        [sys.executable, "-u", "-c",
         "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready'); time.sleep(60)"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert await asyncio.to_thread(child.stdout.readline) == "ready\n"
        assert await installed_smoke.stop_owned_process(child, timeout=0.05) is True
        assert child.poll() == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            await asyncio.to_thread(child.wait)
        child.stdout.close()


def test_container_command_uses_pinned_image_and_disposable_loopback_mount(tmp_path):
    config = tmp_path / "run-only-this-config"
    command = installed_smoke.container_command(config, 28177, "health-bridge-smoke-test")

    assert command == [
        "docker", "run", "--rm", "--pull=never", "--name", "health-bridge-smoke-test",
        "--network", "bridge", "--publish", "127.0.0.1:28177:8123",
        "--volume", f"{config}:/config:rw",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "ghcr.io/home-assistant/home-assistant@sha256:d8922685169707fd91e8b9729902d975f06157d005e422874d201e0261dda196",
    ]


async def test_container_shutdown_targets_named_container_and_reaps_client(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    calls = []

    def stop_container(command, **kwargs):
        calls.append(command)
        child.terminate()
        return subprocess.CompletedProcess(command, 0, "health-bridge-smoke-test\n", "")

    monkeypatch.setattr(installed_smoke.subprocess, "run", stop_container)
    try:
        assert await installed_smoke.stop_owned_container(child, "health-bridge-smoke-test") is False
        assert child.returncode == -signal.SIGTERM
        assert calls == [["docker", "stop", "--time", "40", "health-bridge-smoke-test"]]
    finally:
        if child.poll() is None:
            child.kill()
            await asyncio.to_thread(child.wait)


def test_recorder_count_uses_container_filesystem_while_container_runs(monkeypatch, tmp_path):
    recorder = tmp_path / "home-assistant_v2.db"
    recorder.write_text("host view unavailable during container write")
    calls = []

    def read_inside_container(command, **kwargs):
        calls.append(command)
        return "7\n"

    monkeypatch.setattr(installed_smoke.subprocess, "check_output", read_inside_container)
    assert installed_smoke.recorder_old_states(recorder, 1234, "health-bridge-smoke-test") == 7
    assert calls[0][:3] == ["docker", "exec", "health-bridge-smoke-test"]
    assert calls[0][-1] == "1234"


def test_recorder_count_reads_local_database_without_container(tmp_path):
    recorder = tmp_path / "home-assistant_v2.db"
    with sqlite3.connect(recorder) as db:
        db.execute("CREATE TABLE states (last_updated_ts REAL)")
        db.executemany("INSERT INTO states VALUES (?)", [(1233,), (1234,), (1235,)])

    assert installed_smoke.recorder_old_states(recorder, 1234, None) == 1


async def test_already_exited_container_client_still_gets_a_shutdown_record(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    await asyncio.to_thread(child.wait)
    log = tmp_path / "process.log"
    log.write_text("Home Assistant Core finish process exit code 0\n")

    record = await installed_smoke.record_owned_shutdown(child, "health-bridge-smoke-test", log, 0)

    assert record["returncode"] == 0
    assert record["core_returncode"] == 0
    assert record["forced_kill"] is False


def test_container_gate_rejects_core_failure_when_docker_exits_zero(tmp_path):
    log = tmp_path / "process.log"
    first_run = "Home Assistant Core finish process exit code 0\n"
    log.write_text(first_run + "Home Assistant Core finish process exit code 1\n")
    second_core_exit = installed_smoke.read_core_exit_code(log, len(first_run.encode()))
    shutdowns = [
        {"returncode": 0, "core_returncode": 0, "forced_kill": False},
        {"returncode": 0, "core_returncode": second_core_exit, "forced_kill": False},
    ]

    assert second_core_exit == 1
    assert installed_smoke.shutdowns_clean(shutdowns, "container") is False


def test_container_gate_requires_two_shutdown_records():
    one_clean_exit = [{"returncode": 0, "core_returncode": 0, "forced_kill": False}]

    assert installed_smoke.shutdowns_clean(one_clean_exit, "container") is False


def test_container_gate_accepts_two_clean_docker_and_core_exits():
    clean_exit = {"returncode": 0, "core_returncode": 0, "forced_kill": False}

    assert installed_smoke.shutdowns_clean([clean_exit, clean_exit.copy()], "container") is True


def test_container_gate_requires_exact_clean_fork_sha():
    report = {"fork_sha": "expected", "working_tree_dirty": False}

    installed_smoke.assert_expected_source(report, "container", "expected")
    with pytest.raises(AssertionError):
        installed_smoke.assert_expected_source(report, "container", None)
    with pytest.raises(AssertionError):
        installed_smoke.assert_expected_source(report, "container", "other")
    with pytest.raises(AssertionError):
        installed_smoke.assert_expected_source({**report, "working_tree_dirty": True},
                                               "container", "expected")


def test_installed_gate_requires_every_owner_transition():
    required = {
        "unbound_rejected", "first_admin_approval", "wrong_phone_rejected",
        "transfer_admin_approval", "revoked_phone_rejected",
        "old_only_original_preserved", "same_uuid_reuploaded_and_deleted",
        "exact_receipt_retry", "backup_restored_owner_and_archive",
    }
    report = {"checks": {key: True for key in required}}
    installed_smoke.assert_owner_checks(report)
    for key in required:
        incomplete = {"checks": {**report["checks"], key: False}}
        with pytest.raises(AssertionError, match=key):
            installed_smoke.assert_owner_checks(incomplete)
