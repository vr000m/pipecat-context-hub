# Task: Bound `serve` Process Lifetime to Its Client

**Status**: Complete (v0.0.18)
**Assigned to**: vr000m
**Priority**: Medium
**Branch**: `feature/serve-orphan-watchdog`
**Created**: 2026-04-21
**Completed**: 2026-04-21

## Objective

Make `pipecat-context-hub serve` exit cleanly when its MCP client (Claude Code, Cursor, etc.) goes away, so orphan processes do not accumulate across user sessions and contend on the same Chroma/SQLite index.

## Context

Each MCP-capable editor session that uses the hub spawns its own `serve` child over stdio — that's how MCP works, and it's correct. The defect is that when a client disappears without gracefully closing its end of the stdio pipe, the child blocks indefinitely on `read()` and is never reaped.

Observed today (2026-04-21) on the user's machine: 10+ live `serve` parent/child pairs across multiple Claude sessions, all holding open the same `~/.pipecat-context-hub` directory. Symptoms in the user's koda session: a single transparent "Reconnected to pipecat-context-hub" event mid-session and a small risk of file/handle contention.

Two failure modes drive orphans:

1. **Stdin EOF not propagated.** When the client exits cleanly, the OS closes the pipe and the child sees EOF. The MCP SDK's `stdio_server` should already exit on EOF, but we have not confirmed end-to-end that `asyncio.run(run_stdio(server))` returns and the `try/finally` runs. Verify this works.
2. **Client orphans the FDs.** When the client crashes or is killed without closing FDs (or stops talking to a hub it spawned earlier without closing the pipe), the child's stdin remains open from the kernel's POV. EOF never arrives. The hub sits forever.

For mode (2) the only portable signal we can rely on is **PPID change**: when the parent dies, the child is reparented to PID 1 (`launchd` on macOS, `init`/`systemd` on Linux). Polling `os.getppid()` and exiting on change is two lines, fully portable, and survives the case where stdin stays open indefinitely.

This is a hub-side fix, not a client-side workaround. The accumulating-zombies pattern will affect every long-lived editor that spawns and abandons MCP children.

## Requirements

- `serve` exits cleanly when its stdio client closes the pipe (stdin EOF) — verified by an integration test.
- `serve` exits cleanly within ~3s when its parent process dies and it is reparented to `init`/`launchd`.
- Exit path always closes the `IndexStore` (Chroma + SQLite handles released).
- No new long-lived background threads or asyncio tasks beyond the watchdog itself.
- Watchdog logs a single INFO line on triggered exit (`reason=parent_died original_ppid=N current_ppid=1`) so operators can grep MCP traces.
- No behaviour change when the client is healthy — watchdog is a no-op while `os.getppid() == original_ppid`.
- Cross-platform: works on macOS, Linux, and Windows. Implementation MUST NOT use Unix-only signals (`prctl`, `kqueue`) or write a parallel stdin reader (would race the MCP SDK's read loop and steal protocol bytes).
- Configurable poll interval via env var (default 2s) — primarily for tests, not user-facing.

## Review Focus

- **Stdin EOF path**: confirm the existing `serve_stdio` chain propagates EOF up through `asyncio.run` and the `try/finally`. If it doesn't, the dev plan's mode (1) verification expands to a code change. Inspect `transport.py:14-19` and the MCP SDK's `stdio_server` implementation in the venv.
- **PPID watchdog placement**: should run inside `run_stdio` (so it's bound to the asyncio loop and cancelled when the loop exits), not in `cli.serve`. Cancel the watchdog task when `server.run` returns to avoid a 2s shutdown delay on clean exit.
- **Windows behaviour**: `os.getppid()` exists on Windows but the orphan-reparent semantics differ — orphaned children on Windows do not get a new PPID, the original PPID just becomes invalid. Test or document the gap honestly. If we cannot detect it on Windows, log a warning at startup and rely on stdin EOF alone there.
- **Cancellation safety**: when the watchdog triggers exit, do not just `os._exit()` — that skips the `IndexStore.close()` finally block. Raise `SystemExit` from the main task or cancel the server's run task and let the context manager unwind.
- **Race with clean shutdown**: if the parent dies *during* a tool call, the watchdog fires while a request handler is mid-execution. Verify that cancelling the server task doesn't corrupt the index (writes are read-only at this layer; should be safe).
- **Test isolation**: the integration test must spawn `serve` as a real subprocess and kill its parent (a wrapper) — running in-process won't reproduce the reparenting.

## Implementation Plan

### Phase 1 — Verify stdin EOF path (no code change expected)

Write a subprocess-level test: spawn `pipecat-context-hub serve`, send a valid `initialize` request, then close stdin. Assert process exits with code 0 within 5s. If this passes, mode (1) is already handled by the MCP SDK — proceed to Phase 2. If it hangs, we have a real bug to fix in `run_stdio` (likely a missing `asyncio.shield` or swallowed `EOFError`).

### Phase 2 — Add PPID watchdog

Edit `src/pipecat_context_hub/server/transport.py`:

- Add `_watch_parent(original_ppid: int, interval: float, cancel: asyncio.Event)` coroutine that polls `os.getppid()` every `interval` seconds and sets `cancel` when it changes.
- Modify `run_stdio` to spawn the watchdog and the server concurrently (`asyncio.gather` with `return_exceptions=False`, or `asyncio.wait(..., FIRST_COMPLETED)` and cancel the loser).
- On parent-death detection, log `parent_died original_ppid=N current_ppid=M` at INFO and trigger graceful shutdown by cancelling the server task. The `async with stdio_server()` context manager exits, `run_stdio` returns, `serve_stdio` returns, and `cli.serve`'s `finally` closes the index.
- Read interval from `PIPECAT_HUB_PARENT_WATCH_INTERVAL` env var (default `2.0`). Hidden flag — for tests, not docs.

### Phase 3 — Tests

- `tests/integration/test_serve_lifetime.py`:
  - `test_stdin_close_exits_cleanly` — spawn subprocess, send initialize, close stdin, assert exit ≤5s.
  - `test_parent_death_exits_cleanly` — spawn a Python wrapper that itself spawns `serve` and then exits. Assert the orphaned `serve` exits within ~5s. Skip on Windows with a clear `pytest.skip` reason if we cannot reliably reproduce reparenting.
- Unit test for `_watch_parent` cancellation semantics (no real subprocess): instantiate, set the cancel event, await — assert it returns without polling.

### Phase 4 — Docs

- `CHANGELOG.md` — `### Fixed` entry under v0.0.18 (or whatever the next version is): "serve no longer accumulates as orphan processes when its MCP client exits without closing stdio."
- `CLAUDE.md` Cross-Encoder section is unaffected. Add a short note under a new "Process lifetime" subsection or fold into the existing Troubleshooting section in `docs/README.md`.
- AGENTS.md smoke test #42: spawn `serve`, kill parent shell, confirm `serve` exits within 5s.

## Out of Scope

- **PID file / lock file** — would prevent multiple legitimate concurrent sessions and add filesystem race surface. Not solving today's problem.
- **Single-instance enforcement** — same reason. Multiple Claude windows must each get their own hub.
- **`SIGCHLD` / `prctl(PR_SET_PDEATHSIG)`** — Linux-only; we want one portable mechanism.
- **Reaper that walks `/proc` for orphans on next start** — symptom-treating; doesn't help orphans that exist between starts.

## Open Questions

- ~~Does the existing `serve_stdio` actually exit cleanly on stdin EOF today?~~ **Answered**: yes — verified by `test_stdin_close_exits_cleanly` (process exits in ~0.1s on stdin close). Phase 2 watchdog covers the orphan case where stdin stays open.
- Windows behaviour of orphaned MCP children: watchdog is disabled there (`sys.platform != "win32"` gate), and the integration test is skipped. Stdin EOF still works on Windows. Add a real fix when a user reports the case.

## Codex Review Findings (addressed before merge)

Codex flagged two real defects in the initial implementation:

1. **P1 — Startup-window blind spot.** The PPID was being snapshotted inside `run_stdio`, *after* `cli.serve` spent several seconds opening the IndexStore, loading the embedding model, resolving the reranker, and loading the deprecation map. A client death during that window would have already reparented the process to `init`, so the watchdog locked in the reparented PID as its baseline and never fired. **Fix:** snapshot `os.getppid()` at the top of `cli.serve` before any slow work, thread it through `serve_stdio` → `run_stdio` as `original_ppid`. `transport.py:14-19`, `cli.py` (near `def serve`).
2. **P2 — Integration test didn't exercise the watchdog path.** The original `test_orphaned_serve_exits_via_watchdog` used `os._exit(0)` in the wrapper, which closes the wrapper's pipe descriptors — the child then saw stdin EOF and exited through the pre-existing EOF path, *not* the new watchdog. The test would have passed even with a broken watchdog. **Fix:** the test now creates an `os.pipe()` and spawns a separate "holder" subprocess that inherits the write-end via `pass_fds`, so `serve`'s stdin stays open after the wrapper dies. A sanity assertion confirms the holder is alive when the watchdog should fire. Cleanup kills the holder in `finally`.

While implementing the P2 fix, a third defect surfaced:

3. **`stdio_server` cleanup hang.** Cancelling the MCP `server_task` is not enough — MCP's `stdio_server` uses an anyio TaskGroup containing a `stdin_reader` blocked on `async for line in stdin`. That reader only unblocks when stdin closes. So when the watchdog fired, the log line appeared but the `async with stdio_server()` context manager hung forever waiting for its TaskGroup. **Fix:** after logging the shutdown reason, forcibly `os.close(sys.stdin.fileno())` so the reader sees EOF and the TaskGroup unwinds cleanly. Without this, the watchdog "fired" but the process never actually exited.

## Known Gap: `uv run` wrapper

The watchdog polls the *immediate* PPID. When `serve` is launched via `uv run pipecat-context-hub serve` (the default invocation in this project's docs and most MCP-client configs), `uv` stays alive as an intermediate parent. When the outer client dies, `uv` is reparented to init but does not itself exit. Python's `getppid()` therefore never flips — the watchdog does not fire.

Real-world impact: the 10+ zombie pairs that motivated this fix all ran under `uv run`. This PR does **not** clean those up. It does prevent accumulation in deployments where Python is launched directly (e.g. MCP config pointing at `.venv/bin/pipecat-context-hub serve` with `exec`).

Follow-up options for the `uv` case:
- ~~Walk the ancestor chain at each poll tick~~ — deferred (heavy, fragile).
- ~~Teach `uv run` upstream~~ — out of our control.
- ~~Add an idle-timeout in `serve`~~ — **shipped in this PR** (Phase 5 below).

## Phase 5 — Idle-timeout backstop + MCP-client configuration docs

Added in the same PR after the Codex-review fixes:

**Idle-timeout backstop:**
- `IdleTracker` in `server/transport.py`: monotonic-clock-backed `touch()` / `seconds_since_last()`.
- `_watch_idle(tracker, timeout, interval)` coroutine: returns when idle window exceeded.
- `cli.serve` constructs one `IdleTracker`, passes it to both `create_server` (for the call_tool dispatcher to `touch()`) and `serve_stdio` (for the watchdog).
- `run_stdio` spawns the idle-watch task alongside the parent-death watchdog and the MCP server. Same FIRST_COMPLETED gather + stdin-close shutdown unwind as the parent-death path.
- Default 1800s (30 min). Env var `PIPECAT_HUB_IDLE_TIMEOUT_SECS=0` disables. Documented in `docs/README.md`.
- This catches the **actual production failure mode**: a client that stays alive but stops using a hub it spawned without closing the pipe (the case responsible for the 10+ zombies under `uv run`).

**MCP-client configuration docs:**
- New "MCP Client Configuration" section in `docs/README.md` with two JSON examples:
  - Direct `.venv/bin/pipecat-context-hub` invocation (recommended; parent-death watchdog fires instantly).
  - `uv run` (convenient; idle-timeout backstop catches orphans within 30 min).
- Trade-off explained inline so users can pick the right one for their workflow.

Tests added:
- 5 `_resolve_idle_timeout` env-parsing tests
- 2 `IdleTracker` invariant tests
- 2 `_watch_idle` coroutine tests
- 1 integration test (`test_idle_timeout_exits_serve`): real subprocess via `uv run` with `IDLE_TIMEOUT_SECS=3` and the parent-death watchdog suppressed (interval=3600), proves the idle path exits the orphan in isolation. **This is the test that demonstrates the `uv run` zombie problem is actually solved.**

## Phase 6 — Linux hard-exit backstop

After Phase 5 merged locally, CI on Linux still failed both lifetime integration tests. Stderr tails showed `Shutting down: idle_timeout …` on time, then 15 s of silence — the watchdog fired, graceful teardown started, but `serve` never exited. Diagnosis via breadcrumb logging pinpointed the hang: `mcp.stdio_server` parks its stdin reader in `anyio.to_thread.run_sync(readline, cancellable=False)`. On Linux, once the worker is blocked in `read(0)`, nothing — not `stdio_server.__aexit__`, not CPython's `threading._shutdown()` — can interrupt it. The async unwind returns cleanly; interpreter shutdown then joins the stuck reader forever.

Fix:
- `run_stdio` arms a daemon timer thread (`_HARD_EXIT_TIMEOUT_SECS = 2.5`) the moment a watchdog fires. If graceful unwind completes, `graceful_done` disarms it; if not, the timer calls `on_watchdog_shutdown` (`_SHUTDOWN_CB_TIMEOUT_SECS = 1.0` budget) then `os._exit(0)`.
- After graceful unwind succeeds, `run_stdio` invokes `on_watchdog_shutdown` inline (under the armed timer) and then calls `os._exit(0)` itself — before `stdio_server.__aexit__` can hang. The exit happens from the main thread so it is guaranteed to execute even under GIL-holding C code.
- A `threading.Lock` + `shutdown_cb_started` flag wraps both call sites in `_invoke_shutdown_cb_once`, so the graceful path and the timer thread cannot both enter `IndexStore.close()` concurrently — if the graceful-path call is itself what hangs, the timer short-circuits straight to `os._exit(0)`.
- `exit_on_watchdog_shutdown=True` kwarg opts the CLI into the `os._exit` behaviour; in-process callers (unit tests with a mocked `stdio_server`) default to `False` and continue to receive the `str | None` shutdown reason via return.
- Renamed `on_hard_exit` → `on_watchdog_shutdown` (kwarg + cli closure) to reflect that the callback runs on both graceful and hard paths.

Review loop:
- `/deep-review` flagged the concurrent-callback race as Critical; the single-shot guard addressed it.
- Minor follow-ups (magic constants extracted; `__main__` test-rationale comment removed from `cli.py`; re-entrancy expectation documented on the guard) landed as cleanup.

Open follow-ups (intentionally deferred):
- Promote `shutdown_reason` from `str | None` to a `ShutdownReason` enum. Caller currently does not consume the return value; enum churn is not justified until it does.
- Reconsider `exit_on_watchdog_shutdown: bool` if another in-process caller appears. Today it is the only seam that lets unit tests drive `run_stdio` without being killed — documented as a policy flag, not a test shim.
- Add a subprocess-level guard for `exit_on_watchdog_shutdown=False` once a real in-process caller (library embedder, in-proc dashboard hub, etc.) exists. Today the unit guard (`test_safe_mode_does_not_close_stdin_or_exit`) mocks `stdio_server`, which is sufficient for the contract we ship but cannot catch a regression where the real Linux `stdio_server.__aexit__` hangs in safe mode. The updated docstring explicitly makes that the caller's problem — add a real guard when there is a real caller to speak for.

## Phase 7 — Post-Phase-6 Codex review fixes

A follow-up `/codex:review` on the branch flagged two correctness gaps in the Phase-5/6 implementation. Both fixed in the same PR.

1. **P2 — Idle watchdog could reap an in-flight request.** `call_tool` only called `IdleTracker.touch()` on entry. A slow first `search_*` / `get_code_snippet` that waited on `EmbeddingService` or cross-encoder lazy load could exceed `PIPECAT_HUB_IDLE_TIMEOUT_SECS` and be killed mid-response. **Fix:** `IdleTracker` gained an active counter (`begin()` / `end()`) and `seconds_since_last()` returns `0.0` while any call is in flight. `call_tool` wraps handler dispatch in `begin()` / `try` / `finally: end()` so the counter is decremented even on exceptions. `end()` also touches the clock so the idle window resets at "request finished", not "request dispatched". Regression-guarded by `TestIdleTracker::test_begin_marks_tracker_active_regardless_of_clock` and `TestWatchIdle::test_in_flight_call_suppresses_idle_fire`.
2. **P3 — `exit_on_watchdog_shutdown=False` did not keep in-process callers safe.** The flag only guarded the final `os._exit(0)`. A watchdog fire still closed `sys.stdin` and armed `_hard_exit_on_hang`, which would `os._exit(0)` 2.5 s later — either could tear down the host process of an in-process caller. **Fix:** the flag now gates *every* host-affecting action. The exit branch runs the stdin close + hard-exit timer + `os._exit(0)` as before; a new in-process branch cancels tasks, invokes the shutdown callback once (shared single-shot guard, now lifted out of the exit branch), and returns `shutdown_reason` so the caller can drive its own teardown. Regression-guarded by `TestRunStdioWatchdogWiring::test_safe_mode_does_not_close_stdin_or_exit`.
