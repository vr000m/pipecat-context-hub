"""Unit tests for the stdio transport's parent-death + idle watchdogs.

Env-var resolution is tested in tests/unit/test_config.py since the
ServerConfig computed properties own that logic post-refactor.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, cast
from unittest.mock import patch

import pytest

from pipecat_context_hub.server import transport
from pipecat_context_hub.shared.tracking import IdleTracker


class TestWatchParent:
    @pytest.mark.asyncio
    async def test_returns_when_ppid_changes(self) -> None:
        """Simulate parent death by mocking getppid to return a different PID."""
        original = 12345
        with patch.object(os, "getppid", return_value=99999):
            result = await asyncio.wait_for(
                transport._watch_parent(original, interval=0.01),
                timeout=1.0,
            )
        assert "parent_died" in result
        assert "original_ppid=12345" in result
        assert "current_ppid=99999" in result

    @pytest.mark.asyncio
    async def test_polls_while_ppid_stable(self) -> None:
        """Watchdog must not return as long as PPID is stable; cancellable."""
        original = os.getppid()
        task = asyncio.create_task(transport._watch_parent(original, interval=0.01))
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestIdleTracker:
    def test_starts_at_zero_seconds_idle(self) -> None:
        t = IdleTracker()
        assert t.seconds_since_last() < 0.5  # essentially zero

    def test_touch_resets_clock(self) -> None:
        import time as _time

        t = IdleTracker()
        _time.sleep(0.05)
        assert t.seconds_since_last() >= 0.05
        t.touch()
        assert t.seconds_since_last() < 0.05

    def test_begin_marks_tracker_active_regardless_of_clock(self) -> None:
        """In-flight calls must keep seconds_since_last at 0 — otherwise a
        slow handler (e.g. cold EmbeddingService load) would be reaped
        by the idle watchdog mid-response.
        """
        import time as _time

        t = IdleTracker()
        t.begin()
        # Force the tracker to "look" stale; with an active call, the
        # consumer must still see 0.
        t._last = _time.monotonic() - 100.0
        assert t.seconds_since_last() == 0.0
        t.end()
        # After end(), the clock is fresh again (end() touches).
        assert t.seconds_since_last() < 0.05

    def test_nested_begin_requires_matching_ends(self) -> None:
        t = IdleTracker()
        t.begin()
        t.begin()
        t._last = 0.0  # simulate stale clock
        assert t.seconds_since_last() == 0.0
        t.end()
        # One call still active.
        assert t.seconds_since_last() == 0.0
        t.end()
        # All calls finished — end() touched the clock, so we're fresh.
        assert t.seconds_since_last() < 0.05

    def test_end_without_begin_is_safe(self) -> None:
        """Defensive: stray end() must not underflow or raise."""
        t = IdleTracker()
        t.end()
        assert t._active == 0


class TestWatchIdle:
    @pytest.mark.asyncio
    async def test_returns_when_timeout_exceeded(self) -> None:
        t = IdleTracker()
        # Force tracker to "look" stale by reaching in directly — avoids
        # sleeping the test for the full timeout window.
        import time as _time

        t._last = _time.monotonic() - 100.0
        result = await asyncio.wait_for(
            transport._watch_idle(t, timeout=10.0, interval=0.01),
            timeout=1.0,
        )
        assert "idle_timeout" in result
        assert "timeout_seconds=10" in result

    @pytest.mark.asyncio
    async def test_does_not_return_while_active(self) -> None:
        t = IdleTracker()
        task = asyncio.create_task(transport._watch_idle(t, timeout=10.0, interval=0.01))
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_in_flight_call_suppresses_idle_fire(self) -> None:
        """With `begin()` active and the clock forced stale, the idle
        watchdog must NOT fire — this is the P2 regression guard.
        """
        import time as _time

        t = IdleTracker()
        t.begin()
        t._last = _time.monotonic() - 100.0  # would normally fire
        task = asyncio.create_task(transport._watch_idle(t, timeout=10.0, interval=0.01))
        await asyncio.sleep(0.1)
        assert not task.done(), "idle watchdog fired during an in-flight call"
        # Ending the call resets the clock, so the watchdog remains quiet.
        t.end()
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.skipif(sys.platform == "win32", reason="watchdog disabled on win32")
class TestRunStdioWatchdogWiring:
    """Verify run_stdio exits when its parent disappears, by stubbing the
    stdio_server context and the server.run coroutine to a long-sleep.

    The watchdog should fire and cancel the long-sleep before the test
    timeout. This exercises the wiring without touching real subprocesses.
    """

    @pytest.mark.asyncio
    async def test_watchdog_cancels_server_task(self) -> None:
        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[tuple[None, None]]:
            yield (None, None)

        with patch.object(transport, "stdio_server", fake_stdio_server):

            class FakeServer:
                def create_initialization_options(self) -> object:
                    return object()

                async def run(self, *_args: object, **_kwargs: object) -> None:
                    await asyncio.sleep(60)

            # Flip getppid to a different value after the first poll fires.
            ppid_calls = {"n": 0}
            real_ppid = os.getppid()

            def flipping_ppid() -> int:
                ppid_calls["n"] += 1
                return real_ppid if ppid_calls["n"] <= 1 else 1

            with patch.object(os, "getppid", side_effect=flipping_ppid):
                await asyncio.wait_for(
                    transport.run_stdio(
                        cast(Any, FakeServer()),
                        original_ppid=real_ppid,
                        parent_watch_interval_secs=0.02,
                    ),
                    timeout=5.0,
                )

    @pytest.mark.asyncio
    async def test_graceful_shutdown_disarms_hard_exit_timer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a clean watchdog-triggered unwind, `os._exit` must not
        fire from the backstop thread — otherwise the test runner (or
        any in-process host) would be killed 2.5s later.
        """
        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[tuple[None, None]]:
            yield (None, None)

        exit_calls: list[int] = []
        monkeypatch.setattr(os, "_exit", exit_calls.append)
        # Don't actually close pytest's stdin FD (this test runs
        # in-process and would otherwise fight the runner).
        monkeypatch.setattr(os, "close", lambda _fd: None)

        with patch.object(transport, "stdio_server", fake_stdio_server):

            class FakeServer:
                def create_initialization_options(self) -> object:
                    return object()

                async def run(self, *_args: object, **_kwargs: object) -> None:
                    await asyncio.sleep(60)

            ppid_calls = {"n": 0}
            real_ppid = os.getppid()

            def flipping_ppid() -> int:
                ppid_calls["n"] += 1
                return real_ppid if ppid_calls["n"] <= 1 else 1

            with patch.object(os, "getppid", side_effect=flipping_ppid):
                await asyncio.wait_for(
                    transport.run_stdio(
                        cast(Any, FakeServer()),
                        original_ppid=real_ppid,
                        parent_watch_interval_secs=0.02,
                    ),
                    timeout=5.0,
                )
        # Wait past the 2.5s backstop window; graceful_done.set() should
        # have disarmed the timer so os._exit stays uncalled.
        await asyncio.sleep(3.0)
        assert exit_calls == [], (
            f"hard-exit timer fired after graceful shutdown: {exit_calls}"
        )

    @pytest.mark.asyncio
    async def test_safe_mode_does_not_close_stdin_or_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`exit_on_watchdog_shutdown=False` must not close stdin, must
        not arm the hard-exit timer, and must not call `os._exit`.

        In-process callers own `sys.stdin` and the host process — the
        safe-mode flag is their guarantee that `run_stdio` has no
        host-side effects beyond cancelling its own asyncio tasks and
        invoking the shutdown callback. This is the P3 regression guard.
        """
        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[tuple[None, None]]:
            yield (None, None)

        exit_calls: list[int] = []
        close_calls: list[int] = []
        monkeypatch.setattr(os, "_exit", exit_calls.append)
        monkeypatch.setattr(os, "close", lambda fd: close_calls.append(fd))

        shutdown_cb_calls: list[str] = []

        def on_shutdown() -> None:
            shutdown_cb_calls.append("called")

        with patch.object(transport, "stdio_server", fake_stdio_server):

            class FakeServer:
                def create_initialization_options(self) -> object:
                    return object()

                async def run(self, *_args: object, **_kwargs: object) -> None:
                    await asyncio.sleep(60)

            ppid_calls = {"n": 0}
            real_ppid = os.getppid()

            def flipping_ppid() -> int:
                ppid_calls["n"] += 1
                return real_ppid if ppid_calls["n"] <= 1 else 1

            with patch.object(os, "getppid", side_effect=flipping_ppid):
                result = await asyncio.wait_for(
                    transport.run_stdio(
                        cast(Any, FakeServer()),
                        original_ppid=real_ppid,
                        parent_watch_interval_secs=0.02,
                        on_watchdog_shutdown=on_shutdown,
                        exit_on_watchdog_shutdown=False,
                    ),
                    timeout=5.0,
                )

        # Wait past the hard-exit window just in case a stray timer
        # survived refactoring.
        await asyncio.sleep(3.0)

        assert result is not None, "run_stdio should surface shutdown_reason in safe mode"
        assert result.startswith("parent_died"), f"unexpected reason: {result}"
        assert exit_calls == [], f"os._exit should not fire in safe mode: {exit_calls}"
        assert close_calls == [], f"os.close(stdin) should not fire in safe mode: {close_calls}"
        assert shutdown_cb_calls == ["called"], (
            f"shutdown callback must still run once in safe mode: {shutdown_cb_calls}"
        )
