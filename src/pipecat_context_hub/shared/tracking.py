"""Runtime tracking helpers shared across server layers.

Separate from ``shared/types.py`` (Pydantic data contracts) because
these are stateful runtime objects, not schema definitions.
"""

from __future__ import annotations

import time


class IdleTracker:
    """Tracks the time since the last MCP tool dispatch.

    Used by ``server/main.py`` (the ``call_tool`` dispatcher, producer)
    and ``server/transport.py`` (the idle watchdog, consumer).

    Also tracks in-flight tool calls via ``begin()`` / ``end()``. While
    any call is active, ``seconds_since_last()`` reports ``0.0`` so the
    idle watchdog cannot reap a healthy request mid-response — e.g. a
    slow cold ``search_*`` that waits on ``EmbeddingService`` /
    cross-encoder lazy load taking longer than
    ``PIPECAT_HUB_IDLE_TIMEOUT_SECS``.

    Single-event-loop semantics: ``touch()`` / ``begin()`` / ``end()`` /
    ``seconds_since_last()`` are called from the same asyncio loop, so
    no lock is needed; int and float read/write are atomic under the
    GIL. ``time.monotonic`` is used so wall-clock changes can't trigger
    spurious idle fires.
    """

    def __init__(self) -> None:
        self._last = time.monotonic()
        self._active = 0

    def touch(self) -> None:
        self._last = time.monotonic()

    def begin(self) -> None:
        self._active += 1
        self._last = time.monotonic()

    def end(self) -> None:
        if self._active > 0:
            self._active -= 1
        self._last = time.monotonic()

    def seconds_since_last(self) -> float:
        if self._active > 0:
            return 0.0
        return time.monotonic() - self._last
