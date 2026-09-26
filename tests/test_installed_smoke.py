"""Owned-process cleanup must not strand a disposable HA process on timeout."""

import asyncio
import signal
import sqlite3
import subprocess
import sys

import installed_smoke


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
