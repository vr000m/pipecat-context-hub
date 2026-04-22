"""Integration tests for `serve` process lifetime.

Verifies the hub does not leak as a zombie when its MCP client either
closes stdio cleanly OR dies without closing FDs (the orphan-reparent
case that motivated the watchdog).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _serve_cmd(direct: bool = False) -> list[str]:
    """Command to launch `serve`.

    Default uses `uv run` (matches production / MCP-client invocation).
    ``direct=True`` launches via the test runner's interpreter
    (``python -m pipecat_context_hub.cli serve``), which makes that
    Python process the immediate child of the test wrapper — required
    for the PPID-watchdog test, because `uv run` keeps a live
    intermediate process that prevents PPID flips from propagating.
    Using ``sys.executable`` avoids hardcoding ``.venv/bin/`` paths
    that vary across UV_PROJECT_ENVIRONMENT, OS conventions, and CI.
    """
    if direct:
        return [sys.executable, "-m", "pipecat_context_hub.cli", "serve"]
    return ["uv", "run", "--directory", str(REPO_ROOT), "pipecat-context-hub", "serve"]


def _initialize_payload() -> bytes:
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    return (json.dumps(msg) + "\n").encode()


def test_stdin_close_exits_cleanly() -> None:
    """Closing stdin must cause `serve` to exit within a few seconds."""
    proc = subprocess.Popen(
        _serve_cmd(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(_initialize_payload())
        proc.stdin.flush()
        time.sleep(2.0)  # let initialize round-trip
        proc.stdin.close()
        rc = proc.wait(timeout=10)
        assert rc == 0, f"expected clean exit, got {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_idle_timeout_exits_serve() -> None:
    """`serve` must exit on its own when no requests arrive within the
    idle window, even if stdin stays open and the parent stays alive
    (the `uv run` failure mode where neither EOF nor PPID watchdog
    fires).
    """
    env = os.environ.copy()
    env["PIPECAT_HUB_IDLE_TIMEOUT_SECS"] = "3"
    # Disable PPID watchdog by setting an absurd interval so we
    # demonstrate the idle path in isolation.
    env["PIPECAT_HUB_PARENT_WATCH_INTERVAL"] = "3600"
    proc = subprocess.Popen(
        _serve_cmd(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(_initialize_payload())
        proc.stdin.flush()
        # Do NOT close stdin, do NOT send any further requests.
        # serve should exit via idle timeout within ~timeout + poll margin.
        rc = proc.wait(timeout=20)
        assert rc == 0, f"expected clean exit, got {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="orphan-reparent semantics differ on Windows")
def test_orphaned_serve_exits_via_watchdog(tmp_path: Path) -> None:
    """Orphan `serve` (parent dies without closing stdio) must exit via watchdog.

    This test must *not* let the child see stdin EOF — otherwise the
    existing stdin-close path would hide a broken watchdog. Strategy:

    1. Wrapper creates a pipe `(r, w)`.
    2. Wrapper spawns a "holder" subprocess that inherits `w` via
       `pass_fds` and just sleeps. The holder exists solely to keep
       the write-end of `serve`'s stdin alive after the wrapper dies.
    3. Wrapper spawns `serve` with `stdin=r`.
    4. Wrapper closes its own copies of `r` and `w`, prints the two
       PIDs, and `os._exit`s. Only the holder and `serve` now hold
       pipe FDs. The holder keeps `w` open, so `serve`'s stdin does
       NOT see EOF when the wrapper dies.
    5. `serve` is reparented to init/launchd. The watchdog must fire
       within `PIPECAT_HUB_PARENT_WATCH_INTERVAL` + one poll margin.

    The test cleans up the holder at the end regardless of outcome.
    """
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        textwrap.dedent(
            f"""
            import os, subprocess, sys, time
            r, w = os.pipe()
            # Holder: inherits `w` via pass_fds, sleeps. Keeps serve's
            # stdin open after wrapper dies — forces the watchdog path,
            # not the stdin-EOF path.
            holder = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                pass_fds=(w,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            env["PIPECAT_HUB_PARENT_WATCH_INTERVAL"] = "0.5"
            proc = subprocess.Popen(
                {_serve_cmd(direct=True)!r},
                stdin=r,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            # Release wrapper's copies so only holder and serve hold the pipe.
            os.close(r)
            os.close(w)
            print(f"{{proc.pid}} {{holder.pid}}", flush=True)
            time.sleep(0.5)
            # Exit without closing serve's pipe end — orphan reparents to init.
            os._exit(0)
            """
        )
    )

    wrapper_proc = subprocess.run(
        [sys.executable, str(wrapper)],
        capture_output=True,
        timeout=15,
        check=True,
    )
    serve_pid_str, holder_pid_str = wrapper_proc.stdout.decode().strip().split()
    serve_pid = int(serve_pid_str)
    holder_pid = int(holder_pid_str)

    try:
        # Sanity: holder must still be alive — otherwise we'd be testing
        # the EOF path, not the watchdog. If this fails the test setup
        # is broken, not the watchdog.
        try:
            os.kill(holder_pid, 0)
        except ProcessLookupError:
            pytest.fail(
                f"holder PID {holder_pid} died unexpectedly — test would "
                "have exercised the stdin-EOF path, not the watchdog"
            )

        # Poll up to 15s for the orphan to exit via the watchdog.
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                os.kill(serve_pid, 0)
            except ProcessLookupError:
                return  # exited as expected, watchdog fired
            time.sleep(0.5)

        # Still alive — fail.
        try:
            os.kill(serve_pid, 9)
        except ProcessLookupError:
            pass
        pytest.fail(
            f"serve PID {serve_pid} still alive 15s after parent died "
            f"(holder PID {holder_pid} kept stdin open, so watchdog must fire)"
        )
    finally:
        # Cleanup holder regardless of pass/fail.
        try:
            os.kill(holder_pid, 9)
        except ProcessLookupError:
            pass
