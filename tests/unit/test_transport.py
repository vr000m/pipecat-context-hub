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
from pipecat_context_hub.shared.types import IdleTracker


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
