"""Integration tests for `serve` process lifetime.

Verifies the hub does not leak as a zombie when its MCP client either
closes stdio cleanly OR dies without closing FDs (the orphan-reparent
case that motivated the watchdog).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def seeded_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A tmp `$HOME` holding a minimal non-empty index.

    `cli.serve` fail-fasts with exit code 2 when the index is empty
    (see `_EXIT_INDEX_UNREADY` in cli.py). The lifetime tests only care
    that `serve` reaches the transport; they don't need real data. This
    fixture seeds one record into `<tmp_home>/.pipecat-context-hub/` so
    subprocesses launched with `env["HOME"]=<tmp_home>` find a usable
    index and proceed into `run_stdio`.
    """
    from pipecat_context_hub.services.embedding import (
        EmbeddingIndexWriter,
        EmbeddingService,
    )
    from pipecat_context_hub.services.index.store import IndexStore
    from pipecat_context_hub.shared.config import (
        EmbeddingConfig,
        StorageConfig,
    )
    from pipecat_context_hub.shared.types import ChunkedRecord

    home = tmp_path_factory.mktemp("serve_lifetime_home")
    data_dir = home / ".pipecat-context-hub"
    storage = StorageConfig(data_dir=data_dir)
    store = IndexStore(storage)
    try:
        writer = EmbeddingIndexWriter(store, EmbeddingService(EmbeddingConfig()))
        record = ChunkedRecord(
            chunk_id="seed-1",
            content="seed record for serve lifetime tests",
            content_type="doc",
            source_url="https://docs.pipecat.ai/seed",
            path="/seed",
            indexed_at=datetime.now(tz=timezone.utc),
            metadata={"title": "seed"},
        )
        asyncio.run(writer.upsert([record]))
    finally:
        store.close()
    return home


def _env_with_home(home: Path, **extra: str) -> dict[str, str]:
    """Copy of os.environ with HOME/USERPROFILE pointing at the seeded dir."""
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    # Skip model pre-warm so lifetime tests aren't sensitive to cold-start
    # latency (embedding + cross-encoder load adds seconds to boot).
    env.setdefault("PIPECAT_HUB_WARMUP", "0")
    env.update(extra)
    return env


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


def _readline_with_timeout(stream, timeout: float) -> bytes:
    """Read a single newline-terminated line from ``stream`` within ``timeout``.

    Uses ``select`` on the underlying FD so we don't block forever when
    the peer hangs mid-startup. Raises ``TimeoutError`` if nothing
    arrives in time. Returns the line (may be empty on EOF).
    """
    import select

    deadline = time.time() + timeout
    buf = b""
    fd = stream.fileno()
    while b"\n" not in buf:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError(f"no line within {timeout}s; got {buf!r}")
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise TimeoutError(f"no line within {timeout}s; got {buf!r}")
        chunk = os.read(fd, 4096)
        if not chunk:
            return buf  # EOF
        buf += chunk
    return buf


def _drain_stderr(proc: "subprocess.Popen[bytes]") -> str:
    """Best-effort read of stderr without blocking. Used for diagnostics."""
    if proc.stderr is None:
        return "(no stderr captured)"
    try:
        import select

        chunks: list[bytes] = []
        while True:
            ready, _, _ = select.select([proc.stderr.fileno()], [], [], 0.1)
            if not ready:
                break
            chunk = os.read(proc.stderr.fileno(), 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode(errors="replace")[-4000:]
    except Exception as exc:
        return f"(failed to drain stderr: {exc})"


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


def test_stdin_close_exits_cleanly(seeded_home: Path) -> None:
    """Closing stdin must cause `serve` to exit within a few seconds."""
    proc = subprocess.Popen(
        _serve_cmd(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env_with_home(seeded_home),
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


def test_idle_timeout_exits_serve(seeded_home: Path) -> None:
    """`serve` must exit on its own when no requests arrive within the
    idle window, even if stdin stays open and the parent stays alive
    (the `uv run` failure mode where neither EOF nor PPID watchdog
    fires).

    The outer ``proc.wait`` timeout is generous because `uv run` +
    chromadb/mcp imports + index open can take 10–15 s on cold Linux
    CI runners. What we're actually measuring is the idle-watchdog fire
    window (3 s idle + 1 s poll = ~4 s), so we read the initialize
    response first to anchor the clock after startup. Without that
    anchor a slow cold start would gobble the whole budget before the
    watchdog ever got a chance to tick.
    """
    env = _env_with_home(
        seeded_home,
        PIPECAT_HUB_IDLE_TIMEOUT_SECS="3",
        # Disable PPID watchdog by setting an absurd interval so we
        # demonstrate the idle path in isolation.
        PIPECAT_HUB_PARENT_WATCH_INTERVAL="3600",
    )
    proc = subprocess.Popen(
        _serve_cmd(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(_initialize_payload())
        proc.stdin.flush()
        # Wait for serve to respond to initialize — this confirms it's
        # past startup and the idle tracker is armed. Fail fast with
        # stderr if it doesn't answer within a generous window.
        try:
            ready_line = _readline_with_timeout(proc.stdout, 45.0)
        except TimeoutError:
            stderr = _drain_stderr(proc)
            pytest.fail(
                "serve did not respond to initialize within 45s "
                f"(startup hang). stderr tail:\n{stderr}"
            )
        assert ready_line, "serve closed stdout before responding"
        # Do NOT close stdin, do NOT send any further requests.
        # serve should exit via idle timeout within ~timeout + poll margin.
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            stderr = _drain_stderr(proc)
            pytest.fail(
                "serve did not exit via idle watchdog within 15s of "
                f"ready-state. stderr tail:\n{stderr}"
            )
        assert rc == 0, f"expected clean exit, got {rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="orphan-reparent semantics differ on Windows")
def test_orphaned_serve_exits_via_watchdog(tmp_path: Path, seeded_home: Path) -> None:
    """Orphan `serve` (parent dies without closing stdio) must exit via watchdog.

    This test must *not* let the child see stdin EOF — otherwise the
    existing stdin-close path would hide a broken watchdog. Strategy:

    1. Wrapper creates two pipes: ``(r, w)`` for serve's stdin and
       ``(out_r, out_w)`` for serve's stdout.
    2. Wrapper spawns a "holder" subprocess that inherits ``w`` and
       ``out_r`` via ``pass_fds`` and just sleeps. Holding ``w`` keeps
       serve's stdin alive; holding ``out_r`` prevents SIGPIPE if serve
       writes anything after the wrapper dies.
    3. Wrapper spawns `serve` with ``stdin=r``, ``stdout=out_w``.
    4. Wrapper sends ``initialize`` on ``w`` and reads the response
       from ``out_r``. This anchors the test clock *after* startup, so
       the 15s poll below measures watchdog latency, not cold-start
       latency (which on Linux CI can itself exceed 15s).
    5. Wrapper closes its own FDs, prints PIDs, and ``os._exit``s.
       Only holder + serve now hold pipe FDs. The holder keeps ``w``
       open, so serve's stdin does NOT see EOF.
    6. `serve` is reparented to init/launchd. The watchdog must fire
       within ``PIPECAT_HUB_PARENT_WATCH_INTERVAL`` + one poll margin.

    The test cleans up the holder at the end regardless of outcome.
    """
    wrapper = tmp_path / "wrapper.py"
    init_payload = _initialize_payload().decode()
    wrapper.write_text(
        textwrap.dedent(
            f"""
            import os, select, subprocess, sys, time
            r, w = os.pipe()
            out_r, out_w = os.pipe()
            # Holder inherits both `w` and `out_r` so serve's stdin stays
            # open (no EOF) AND serve's stdout has a reader (no SIGPIPE)
            # after the wrapper exits.
            holder = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                pass_fds=(w, out_r),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            env["HOME"] = {str(seeded_home)!r}
            env["USERPROFILE"] = {str(seeded_home)!r}
            env["PIPECAT_HUB_PARENT_WATCH_INTERVAL"] = "0.5"
            proc = subprocess.Popen(
                {_serve_cmd(direct=True)!r},
                stdin=r,
                stdout=out_w,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            # Release wrapper's copies of the child ends.
            os.close(r)
            os.close(out_w)
            # Send initialize and wait for response — confirms serve is
            # past startup before we orphan it.
            os.write(w, {init_payload!r}.encode())
            deadline = time.time() + 45
            buf = b""
            while b"\\n" not in buf:
                remaining = deadline - time.time()
                if remaining <= 0:
                    sys.stderr.write(f"wrapper: no initialize response within 45s; buf={{buf!r}}\\n")
                    os._exit(2)
                ready, _, _ = select.select([out_r], [], [], remaining)
                if not ready:
                    continue
                chunk = os.read(out_r, 4096)
                if not chunk:
                    sys.stderr.write("wrapper: serve closed stdout before responding\\n")
                    os._exit(3)
                buf += chunk
            # Wrapper releases its copies; holder keeps w + out_r alive.
            os.close(w)
            os.close(out_r)
            print(f"{{proc.pid}} {{holder.pid}}", flush=True)
            # Exit without closing serve's pipe end — orphan reparents to init.
            os._exit(0)
            """
        )
    )

    try:
        wrapper_proc = subprocess.run(
            [sys.executable, str(wrapper)],
            capture_output=True,
            timeout=60,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        pytest.fail(
            f"wrapper exited with {exc.returncode}. "
            f"stderr:\n{exc.stderr.decode(errors='replace')}"
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

        # Poll up to 15s for the orphan to exit via the watchdog. Serve
        # has already finished startup (initialize response round-tripped
        # in the wrapper), so this is a pure measurement of watchdog
        # latency after the PPID flip.
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
