"""stdio transport adapter for the MCP server."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from typing import Callable

from mcp import stdio_server
from mcp.server.lowlevel import Server

from pipecat_context_hub.shared.tracking import IdleTracker

logger = logging.getLogger(__name__)

# Idle-watchdog poll cap. The actual poll interval is min(this, max(timeout/4, 1.0))
# so very short timeouts (used in tests) still poll frequently enough.
_IDLE_POLL_INTERVAL_SECS = 30.0

# Hard-exit timer budget — how long to wait after graceful_done.set()
# would normally fire before giving up and calling os._exit(0). Covers
# the worst-case asyncio-task-unwind + Chroma close on Linux.
_HARD_EXIT_TIMEOUT_SECS = 2.5

# Per-callback budget inside the hard-exit path. If on_watchdog_shutdown
# hangs here (e.g. Chroma close wedged), we abandon it rather than
# defeating the watchdog — on-disk state is crash-consistent.
_SHUTDOWN_CB_TIMEOUT_SECS = 1.0


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
    on_watchdog_shutdown: Callable[[], None] | None = None,
    exit_on_watchdog_shutdown: bool = False,
) -> str | None:
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

    ``exit_on_watchdog_shutdown`` decides what happens when a watchdog
    fires. True (CLI mode): close stdin, arm a hard-exit timer, and
    call ``os._exit(0)`` after graceful unwind. False (in-process
    mode): no stdin close, no timer, no ``os._exit`` — tasks are
    cancelled, the shutdown callback runs once after cancellation, and
    ``run_stdio`` returns ``shutdown_reason`` so the caller can drive
    its own teardown. See ``serve_stdio`` for the full rationale.
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

        graceful_done: threading.Event | None = None
        # Single-shot guard for the on_watchdog_shutdown callback. Lives
        # outside the `exit_on_watchdog_shutdown` branch because the
        # in-process path below also invokes the callback on the
        # graceful unwind, and we want identical one-shot semantics in
        # both modes.
        shutdown_cb_lock = threading.Lock()
        shutdown_cb_started = [False]

        def _invoke_shutdown_cb_once(context: str) -> None:
            if on_watchdog_shutdown is None:
                return
            with shutdown_cb_lock:
                if shutdown_cb_started[0]:
                    return
                shutdown_cb_started[0] = True
            try:
                on_watchdog_shutdown()
            except Exception:
                logger.exception(
                    "on_watchdog_shutdown raised during %s", context
                )

        if shutdown_reason is not None and exit_on_watchdog_shutdown:
            logger.info("Shutting down: %s", shutdown_reason)
            # Arm a watchdog-of-the-watchdog: a plain OS thread that
            # will hard-exit the process if graceful teardown doesn't
            # complete within a few seconds. This is defensive against
            # two Linux-specific failure modes we've observed in CI:
            #
            # 1. mcp's stdio_server reads stdin via
            #    `anyio.to_thread.run_sync(readline, cancellable=False)`.
            #    On Linux, closing fd 0 does not wake the worker
            #    thread's blocked ``read(0)`` — the kernel keeps the
            #    file object alive via the thread's reference — so the
            #    asyncio task never observes its own cancellation.
            # 2. Nothing in asyncio (``wait_for``, ``wait(timeout=…)``,
            #    ``gather``) guarantees return when the inner task is
            #    stuck in an uninterruptible blocked syscall off-loop;
            #    the event-loop timer fires but the subsequent unwind
            #    path still has to await the un-cancellable task.
            #
            # An OS thread bypasses both: ``time.sleep`` + ``os._exit``
            # is scheduled by the kernel, not the event loop. The
            # watchdog's whole purpose is "client is gone, die", so
            # blocking forever on graceful unwind defeats it.
            # Event signals that graceful teardown finished, disarming
            # the hard-exit thread. Without this, a successful graceful
            # unwind (macOS, or any in-process caller) would still get
            # `os._exit` called 2.5 s later and kill the host — e.g.
            # the pytest worker when `run_stdio` is driven directly.
            #
            # Arming the timer (and closing stdin, below) is gated on
            # `exit_on_watchdog_shutdown` so in-process callers
            # (tests, library embedders) are never exposed to an
            # `os._exit` from a daemon thread or a closed host stdin.
            # The single-shot guard for `on_watchdog_shutdown` is
            # defined above so the in-process `else` path can reuse it.
            graceful_done = threading.Event()

            def _hard_exit_on_hang() -> None:
                # `Event.wait(timeout)` returns True if set before the
                # timeout, False on timeout. Only hard-exit on timeout.
                if graceful_done.wait(_HARD_EXIT_TIMEOUT_SECS):
                    return
                # Write directly to stderr (bypassing the logging
                # framework, which can buffer / deadlock with handlers
                # held by the stuck main thread) so operators still see
                # the shutdown reason.
                try:
                    sys.stderr.write(
                        "pipecat-context-hub: graceful shutdown timed out, "
                        "hard-exiting (stdin reader stuck in uninterruptible "
                        "read(0))\n"
                    )
                    sys.stderr.flush()
                except Exception:  # nosec B110 - best-effort diagnostic before hard exit
                    pass  # nosec B110
                # Give `on_watchdog_shutdown` a short, bounded window.
                # The single-shot guard short-circuits if the graceful
                # path already started the callback (and is hung in
                # it); in that case we skip straight to `os._exit(0)`
                # rather than starting a second concurrent
                # `IndexStore.close()`. If it hangs fresh here, abandon
                # it after 1 s — on-disk state is crash-consistent
                # (SQLite WAL + Chroma recovery on next open).
                cb_done = threading.Event()

                def _run_cb() -> None:
                    try:
                        _invoke_shutdown_cb_once("hard-exit timer")
                    finally:
                        cb_done.set()

                threading.Thread(
                    target=_run_cb,
                    name="hub-hard-exit-cleanup",
                    daemon=True,
                ).start()
                cb_done.wait(_SHUTDOWN_CB_TIMEOUT_SECS)
                os._exit(0)

            threading.Thread(
                target=_hard_exit_on_hang,
                name="hub-hard-exit-timer",
                daemon=True,
            ).start()

            # Still attempt graceful unwind — on macOS/BSD (and
            # client-clean-close paths on Linux) this completes well
            # within the 2.5 s window and the timer thread is
            # harmless.
            try:
                os.close(sys.stdin.fileno())
            except (OSError, ValueError):
                pass
        elif shutdown_reason is not None:
            # In-process safe mode (`exit_on_watchdog_shutdown=False`).
            # The caller owns `sys.stdin` and the host process — do NOT
            # close stdin, do NOT arm the hard-exit timer. Cancel the
            # pending tasks, invoke the shutdown callback once so
            # critical resources still release, and return
            # `shutdown_reason` so the caller can drive its own
            # teardown. On Linux the graceful unwind may hang inside
            # `stdio_server.__aexit__` for the same reasons the timer
            # exists; that is the in-process caller's problem to handle
            # (e.g. test setups mock `stdio_server` to avoid it).
            logger.info("Shutting down: %s", shutdown_reason)
            # Callback runs AFTER cancellation (below) to mirror the
            # exit branch's ordering and avoid racing a still-pending
            # tool call that is mid-read against the IndexStore.

        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        if graceful_done is None and shutdown_reason is not None:
            # In-process safe-mode cleanup: tasks are now cancelled, so
            # it is safe to release index handles.
            _invoke_shutdown_cb_once("graceful unwind (in-process)")

        if graceful_done is not None:
            # Exit path only (`exit_on_watchdog_shutdown=True`). The
            # in-process path above already called
            # `_invoke_shutdown_cb_once` on the graceful unwind and
            # returns normally so the caller can tear down.
            #
            # Release index handles while the hard-exit timer is still
            # armed — Chroma's close can hang on Linux (internal threads
            # we cannot interrupt), and the caller's outer `finally` runs
            # after `run_stdio` returns, outside the timer's scope. If
            # this hangs, the 2.5 s timer fires; the single-shot guard
            # ensures the timer does NOT start a second concurrent
            # close, it goes straight to `os._exit(0)`. If this
            # completes, we disarm the timer below.
            _invoke_shutdown_cb_once("graceful unwind")
            # Graceful path completed — disarm the hard-exit timer so
            # it does not fire after `run_stdio` returns.
            graceful_done.set()

            # Exit before `stdio_server.__aexit__` runs. On Linux, the
            # anyio worker thread doing the cancellable=False
            # `readline` is parked in uninterruptible read(0); both
            # stdio_server's teardown and CPython's interpreter
            # shutdown wait for that thread and hang forever. The
            # watchdog's job is "client is gone, die" — skip both by
            # exiting directly. We do this from the main thread (not
            # the daemon timer) so the call is guaranteed to execute
            # even under GIL-holding C code.
            os._exit(0)

        # Surface server-task exceptions (e.g. unexpected protocol error)
        # while still letting the index_store finally-block run.
        if server_task in done:
            exc = server_task.exception()
            if exc is not None:
                raise exc

        return shutdown_reason


def serve_stdio(
    server: Server,
    original_ppid: int | None = None,
    idle_tracker: IdleTracker | None = None,
    parent_watch_interval_secs: float = 0.0,
    idle_timeout_secs: float = 0.0,
    on_watchdog_shutdown: Callable[[], None] | None = None,
    exit_on_watchdog_shutdown: bool = False,
) -> str | None:
    """Blocking entry point that runs the stdio server.

    ``original_ppid`` should be captured by the caller at process entry
    (before any index/service construction) so that a parent-death that
    happens during startup is still detected by the watchdog.
    ``idle_tracker`` is the request-touch tracker used by the idle
    watchdog; the caller passes the same instance to ``create_server``.
    The two timeouts come from ``ServerConfig`` env-aware computed
    properties; 0 disables the corresponding watchdog.
    ``on_watchdog_shutdown`` is invoked once when a watchdog-triggered
    shutdown begins — either inline on the graceful unwind (while the
    hard-exit timer is armed) or from the timer thread if the graceful
    path hangs. A single-shot guard ensures at most one invocation, so
    a hanging close on the graceful path does not spawn a second
    concurrent close when the timer fires. Pass the index-store close
    here so critical resources are released whether the unwind is
    graceful or hard.

    ``exit_on_watchdog_shutdown`` must be True for the CLI entry point
    and False for any in-process caller (tests, library embedding).
    This is a policy choice, not a test shim: when True, ``run_stdio``
    closes ``sys.stdin`` (to unblock mcp's stdin reader on Linux),
    arms a 2.5 s daemon hard-exit timer, and calls ``os._exit(0)``
    itself after graceful unwind — otherwise, on Linux,
    ``mcp.stdio_server.__aexit__`` waits on the anyio worker thread
    parked in an uninterruptible ``read(0)`` and control never returns.
    When False, every host-affecting action is suppressed: no stdin
    close, no hard-exit timer, no ``os._exit``. The shutdown callback
    still runs (single-shot) so index handles are released, tasks are
    cancelled, and ``run_stdio`` returns ``shutdown_reason`` to the
    caller. In-process callers MUST arrange for the graceful unwind
    actually to complete on their platform (e.g. by mocking
    ``stdio_server``); the safe-mode flag does not rescue them from a
    real Linux ``read(0)`` hang.
    """
    return asyncio.run(
        run_stdio(
            server,
            original_ppid=original_ppid,
            idle_tracker=idle_tracker,
            parent_watch_interval_secs=parent_watch_interval_secs,
            idle_timeout_secs=idle_timeout_secs,
            on_watchdog_shutdown=on_watchdog_shutdown,
            exit_on_watchdog_shutdown=exit_on_watchdog_shutdown,
        )
    )
