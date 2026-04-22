"""Unit tests for the stdio transport's parent-death watchdog."""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

import pytest

from pipecat_context_hub.server import transport


class TestResolveWatchInterval:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(transport._PARENT_WATCH_INTERVAL_ENV, raising=False)
        assert transport._resolve_watch_interval() == transport._DEFAULT_PARENT_WATCH_INTERVAL

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(transport._PARENT_WATCH_INTERVAL_ENV, "0.05")
        assert transport._resolve_watch_interval() == pytest.approx(0.05)

    def test_invalid_value_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(transport._PARENT_WATCH_INTERVAL_ENV, "not-a-number")
        assert transport._resolve_watch_interval() == transport._DEFAULT_PARENT_WATCH_INTERVAL

    def test_negative_clamped_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(transport._PARENT_WATCH_INTERVAL_ENV, "-1")
        assert transport._resolve_watch_interval() == 0.0


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


class TestResolveIdleTimeout:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(transport._IDLE_TIMEOUT_ENV, raising=False)
        assert transport._resolve_idle_timeout() == transport._DEFAULT_IDLE_TIMEOUT_SECS

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(transport._IDLE_TIMEOUT_ENV, "60")
        assert transport._resolve_idle_timeout() == pytest.approx(60.0)

    def test_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(transport._IDLE_TIMEOUT_ENV, "0")
        assert transport._resolve_idle_timeout() == 0.0

    def test_invalid_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(transport._IDLE_TIMEOUT_ENV, "garbage")
        assert transport._resolve_idle_timeout() == transport._DEFAULT_IDLE_TIMEOUT_SECS

    def test_negative_clamped_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(transport._IDLE_TIMEOUT_ENV, "-30")
        assert transport._resolve_idle_timeout() == 0.0


class TestIdleTracker:
    def test_starts_at_zero_seconds_idle(self) -> None:
        t = transport.IdleTracker()
        assert t.seconds_since_last() < 0.5  # essentially zero

    def test_touch_resets_clock(self) -> None:
        import time as _time

        t = transport.IdleTracker()
        _time.sleep(0.05)
        assert t.seconds_since_last() >= 0.05
        t.touch()
        assert t.seconds_since_last() < 0.05


class TestWatchIdle:
    @pytest.mark.asyncio
    async def test_returns_when_timeout_exceeded(self) -> None:
        t = transport.IdleTracker()
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
        t = transport.IdleTracker()
        task = asyncio.create_task(transport._watch_idle(t, timeout=10.0, interval=0.01))
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
    async def test_watchdog_cancels_server_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from collections.abc import AsyncIterator
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[tuple[None, None]]:
            yield (None, None)

        monkeypatch.setattr(transport, "stdio_server", fake_stdio_server)
        monkeypatch.setenv(transport._PARENT_WATCH_INTERVAL_ENV, "0.02")

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
            # Cast through Any — FakeServer fakes the duck-typed surface
            # (create_initialization_options + run) used by run_stdio.
            from typing import Any, cast
            await asyncio.wait_for(
                transport.run_stdio(cast(Any, FakeServer())), timeout=5.0
            )
