"""Owned-process cleanup must not strand a disposable HA process on timeout."""

import asyncio
import signal
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
