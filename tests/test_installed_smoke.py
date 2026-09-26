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
