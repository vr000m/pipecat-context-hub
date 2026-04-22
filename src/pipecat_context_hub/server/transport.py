"""stdio transport adapter for the MCP server."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

from mcp import stdio_server
from mcp.server.lowlevel import Server

logger = logging.getLogger(__name__)

# Hidden env var — for tests, not user-facing. Default 2.0s balances
# responsiveness against wakeups.
_PARENT_WATCH_INTERVAL_ENV = "PIPECAT_HUB_PARENT_WATCH_INTERVAL"
_DEFAULT_PARENT_WATCH_INTERVAL = 2.0

# User-facing — operators can tune. Default 30 minutes covers the
# Claude-holds-pipes-open failure mode (where neither stdin EOF nor PPID
# watchdog fires) without killing genuinely-idle-but-still-needed
# sessions. Set to 0 to disable.
_IDLE_TIMEOUT_ENV = "PIPECAT_HUB_IDLE_TIMEOUT_SECS"
_DEFAULT_IDLE_TIMEOUT_SECS = 1800.0
_IDLE_POLL_INTERVAL_SECS = 30.0


class IdleTracker:
    """Tracks the time since the last MCP tool dispatch.

    Constructed once in ``cli.serve``; passed both to ``create_server``
    (so the tool dispatcher can call ``touch()`` on every request) and
    to ``serve_stdio`` (so the idle watchdog can read the timestamp).
    Uses ``time.monotonic`` so wall-clock changes don't cause spurious
    fires.
    """

    def __init__(self) -> None:
        self._last = time.monotonic()

    def touch(self) -> None:
        self._last = time.monotonic()

    def seconds_since_last(self) -> float:
        return time.monotonic() - self._last


async def _watch_idle(tracker: IdleTracker, timeout: float, interval: float) -> str:
    """Return a reason string when the tracker has been idle for ``timeout`` seconds."""
    while True:
        await asyncio.sleep(interval)
        idle = tracker.seconds_since_last()
        if idle >= timeout:
            return (
                f"idle_timeout idle_seconds={idle:.0f} "
                f"timeout_seconds={timeout:.0f}"
            )


async def _watch_parent(original_ppid: int, interval: float) -> str:
    """Poll for parent-process death; return a reason string when detected.

    Posix: when the parent exits, the child is reparented to PID 1
    (init/launchd), so getppid() flips. Windows lacks the reparent
    semantics — getppid() may return stale PIDs — so the caller skips
    spawning this watchdog there.
    """
    while True:
        await asyncio.sleep(interval)
        current = os.getppid()
        if current != original_ppid:
            return f"parent_died original_ppid={original_ppid} current_ppid={current}"


async def run_stdio(
    server: Server,
    original_ppid: int | None = None,
    idle_tracker: IdleTracker | None = None,
) -> None:
    """Run the MCP server over stdio transport.

    Spawns a parent-death watchdog alongside the MCP loop. If the client
    disappears without closing stdin (e.g. crashed editor that orphans
    its FDs), the watchdog notices reparenting and triggers shutdown so
    the hub does not accumulate as a zombie holding the index.

    ``original_ppid`` is the PPID snapshot to compare against. The caller
    should capture it at process entry (before any slow startup work),
    because startup can take several seconds and the client may die
    during that window — if we snapshotted here, we'd lock in the
    already-reparented PID and the watchdog would never fire.
    """
    logger.info("Starting MCP server on stdio transport")

    interval = _resolve_watch_interval()
    enable_watchdog = sys.platform != "win32" and interval > 0
    if original_ppid is None:
        original_ppid = os.getppid() if enable_watchdog else 0

    idle_timeout = _resolve_idle_timeout()
    enable_idle_watch = idle_tracker is not None and idle_timeout > 0

    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        server_task = asyncio.create_task(
            server.run(read_stream, write_stream, init_options),
            name="mcp-server-run",
        )
        tasks: list[asyncio.Task[object]] = [server_task]
        watchdog_task: asyncio.Task[str] | None = None
        idle_task: asyncio.Task[str] | None = None
        if enable_watchdog:
            watchdog_task = asyncio.create_task(
                _watch_parent(original_ppid, interval),
                name="parent-death-watchdog",
            )
            tasks.append(watchdog_task)
        if enable_idle_watch:
            assert idle_tracker is not None  # type-narrow for mypy
            poll = min(_IDLE_POLL_INTERVAL_SECS, max(idle_timeout / 4.0, 1.0))
            idle_task = asyncio.create_task(
                _watch_idle(idle_tracker, idle_timeout, poll),
                name="idle-watchdog",
            )
            tasks.append(idle_task)

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        shutdown_reason: str | None = None
        if watchdog_task is not None and watchdog_task in done:
            shutdown_reason = watchdog_task.result()
        elif idle_task is not None and idle_task in done:
            shutdown_reason = idle_task.result()

        if shutdown_reason is not None:
            logger.info("Shutting down: %s", shutdown_reason)
            # Cancelling server_task alone is not enough: stdio_server's
            # internal TaskGroup still awaits a stdin_reader that's
            # blocked on `async for line in stdin`. If the client
            # orphaned us without closing the pipe, that reader never
            # unblocks and `async with stdio_server()` hangs on exit.
            # Forcibly close stdin so the reader sees EOF / ClosedResource
            # and the TaskGroup can unwind.
            try:
                os.close(sys.stdin.fileno())
            except OSError:
                pass

        # Surface server-task exceptions (e.g. unexpected protocol error)
        # while still letting the index_store finally-block run.
        if server_task in done:
            exc = server_task.exception()
            if exc is not None:
                raise exc


def serve_stdio(
    server: Server,
    original_ppid: int | None = None,
    idle_tracker: IdleTracker | None = None,
) -> None:
    """Blocking entry point that runs the stdio server.

    ``original_ppid`` should be captured by the caller at process entry
    (before any index/service construction) so that a parent-death that
    happens during startup is still detected by the watchdog.
    ``idle_tracker`` is the request-touch tracker used by the idle
    watchdog; the caller passes the same instance to ``create_server``.
    """
    asyncio.run(
        run_stdio(server, original_ppid=original_ppid, idle_tracker=idle_tracker)
    )


def _resolve_idle_timeout() -> float:
    raw = os.environ.get(_IDLE_TIMEOUT_ENV, "").strip()
    if not raw:
        return _DEFAULT_IDLE_TIMEOUT_SECS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r (not a float); using %.0fs",
            _IDLE_TIMEOUT_ENV,
            raw,
            _DEFAULT_IDLE_TIMEOUT_SECS,
        )
        return _DEFAULT_IDLE_TIMEOUT_SECS
    return max(0.0, value)


def _resolve_watch_interval() -> float:
    raw = os.environ.get(_PARENT_WATCH_INTERVAL_ENV, "").strip()
    if not raw:
        return _DEFAULT_PARENT_WATCH_INTERVAL
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r (not a float); using %.1fs",
            _PARENT_WATCH_INTERVAL_ENV,
            raw,
            _DEFAULT_PARENT_WATCH_INTERVAL,
        )
        return _DEFAULT_PARENT_WATCH_INTERVAL
    return max(0.0, value)
