"""stdio transport adapter for the MCP server."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from mcp import stdio_server
from mcp.server.lowlevel import Server

logger = logging.getLogger(__name__)

# Hidden env var — for tests, not user-facing. Default 2.0s balances
# responsiveness against wakeups.
_PARENT_WATCH_INTERVAL_ENV = "PIPECAT_HUB_PARENT_WATCH_INTERVAL"
_DEFAULT_PARENT_WATCH_INTERVAL = 2.0


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


async def run_stdio(server: Server, original_ppid: int | None = None) -> None:
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

    async with stdio_server() as (read_stream, write_stream):
        init_options = server.create_initialization_options()
        server_task = asyncio.create_task(
            server.run(read_stream, write_stream, init_options),
            name="mcp-server-run",
        )
        tasks: list[asyncio.Task[object]] = [server_task]
        watchdog_task: asyncio.Task[str] | None = None
        if enable_watchdog:
            watchdog_task = asyncio.create_task(
                _watch_parent(original_ppid, interval),
                name="parent-death-watchdog",
            )
            tasks.append(watchdog_task)

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        if watchdog_task is not None and watchdog_task in done:
            logger.info("Shutting down: %s", watchdog_task.result())
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


def serve_stdio(server: Server, original_ppid: int | None = None) -> None:
    """Blocking entry point that runs the stdio server.

    ``original_ppid`` should be captured by the caller at process entry
    (before any index/service construction) so that a parent-death that
    happens during startup is still detected by the watchdog.
    """
    asyncio.run(run_stdio(server, original_ppid=original_ppid))


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
