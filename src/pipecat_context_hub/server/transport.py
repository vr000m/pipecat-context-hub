"""stdio transport adapter for the MCP server."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Callable

from mcp import stdio_server
from mcp.server.lowlevel import Server

from pipecat_context_hub.shared.tracking import IdleTracker

logger = logging.getLogger(__name__)

# Idle-watchdog poll cap. The actual poll interval is min(this, max(timeout/4, 1.0))
# so very short timeouts (used in tests) still poll frequently enough.
_IDLE_POLL_INTERVAL_SECS = 30.0


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


async def run_stdio(
    server: Server,
    original_ppid: int | None = None,
    idle_tracker: IdleTracker | None = None,
    parent_watch_interval_secs: float = 0.0,
    idle_timeout_secs: float = 0.0,
    on_hard_exit: Callable[[], None] | None = None,
) -> None:
    """Run the MCP server over stdio transport.

    Spawns a parent-death watchdog and (optionally) an idle-timeout
    watchdog alongside the MCP loop. If the client disappears without
    closing stdin (e.g. crashed editor that orphans its FDs, or a
    long-lived editor that stops using a hub it spawned), one of the
    watchdogs notices and triggers shutdown so the hub does not
    accumulate as a zombie holding the index.

    ``original_ppid`` is the PPID snapshot to compare against. The
    caller should capture it at process entry (before any slow startup
    work), because startup can take several seconds and the client may
    die during that window — if we snapshotted here, we'd lock in the
    already-reparented PID and the watchdog would never fire.

    ``parent_watch_interval_secs`` and ``idle_timeout_secs`` are
    resolved by the caller (typically from ``ServerConfig`` env-aware
    properties). A value of 0 disables the corresponding watchdog.
    """
    logger.info("Starting MCP server on stdio transport")

    enable_watchdog = sys.platform != "win32" and parent_watch_interval_secs > 0
    if original_ppid is None:
        original_ppid = os.getppid() if enable_watchdog else 0

    enable_idle_watch = idle_tracker is not None and idle_timeout_secs > 0

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
                _watch_parent(original_ppid, parent_watch_interval_secs),
                name="parent-death-watchdog",
            )
            tasks.append(watchdog_task)
        if enable_idle_watch and idle_tracker is not None:
            poll = min(_IDLE_POLL_INTERVAL_SECS, max(idle_timeout_secs / 4.0, 1.0))
            idle_task = asyncio.create_task(
                _watch_idle(idle_tracker, idle_timeout_secs, poll),
                name="idle-watchdog",
            )
            tasks.append(idle_task)

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        shutdown_reason: str | None = None
        if watchdog_task is not None and watchdog_task in done:
            shutdown_reason = watchdog_task.result()
        elif idle_task is not None and idle_task in done:
            shutdown_reason = idle_task.result()

        for task in pending:
            task.cancel()

        if shutdown_reason is not None:
            logger.info("Shutting down: %s", shutdown_reason)
            # Close stdin so stdio_server's internal stdin_reader can
            # observe EOF. This works on macOS/BSD but NOT reliably on
            # Linux: the reader runs inside a thread pool worker via
            # anyio's `to_thread.run_sync(readline, cancellable=False)`,
            # and Linux keeps the kernel file object alive as long as
            # the worker holds it — closing the FD table entry does not
            # wake the blocked `read(0)` syscall. So asyncio can't
            # observe the task's cancellation and `await task` hangs.
            try:
                os.close(sys.stdin.fileno())
            except (OSError, ValueError):
                pass

            # Try a graceful unwind for a short window. If the stdin
            # reader is stuck in a blocked syscall we can't interrupt,
            # hard-exit the process — the watchdog's whole purpose is
            # "the client is gone, die" so waiting forever defeats it.
            # The caller's `finally` won't run, so the caller is
            # expected to have no critical state open at this point
            # (serve.py closes the index_store in its outer finally
            # after serve_stdio returns; if we hard-exit here, Chroma's
            # SQLite WAL is fsync'd on its own close which runs via
            # atexit handlers too).
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=2.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Graceful shutdown timed out; hard-exiting "
                    "(stdin reader thread stuck in blocked syscall)"
                )
                if on_hard_exit is not None:
                    try:
                        on_hard_exit()
                    except Exception:
                        logger.exception("on_hard_exit callback raised")
                # flush before hard exit so operators see the last log
                for handler in logging.getLogger().handlers:
                    try:
                        handler.flush()
                    except Exception:
                        pass
                os._exit(0)
        else:
            # Normal teardown (e.g. stdin EOF from client-driven shutdown).
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
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
    parent_watch_interval_secs: float = 0.0,
    idle_timeout_secs: float = 0.0,
    on_hard_exit: Callable[[], None] | None = None,
) -> None:
    """Blocking entry point that runs the stdio server.

    ``original_ppid`` should be captured by the caller at process entry
    (before any index/service construction) so that a parent-death that
    happens during startup is still detected by the watchdog.
    ``idle_tracker`` is the request-touch tracker used by the idle
    watchdog; the caller passes the same instance to ``create_server``.
    The two timeouts come from ``ServerConfig`` env-aware computed
    properties; 0 disables the corresponding watchdog.
    ``on_hard_exit`` is invoked before ``os._exit`` when a watchdog
    shutdown cannot unwind gracefully (e.g. Linux pipe-reader stuck in
    a blocked syscall); pass the index-store close here so critical
    resources are released even on the hard path.
    """
    asyncio.run(
        run_stdio(
            server,
            original_ppid=original_ppid,
            idle_tracker=idle_tracker,
            parent_watch_interval_secs=parent_watch_interval_secs,
            idle_timeout_secs=idle_timeout_secs,
            on_hard_exit=on_hard_exit,
        )
    )
