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
- Walk the ancestor chain at each poll tick and shut down if any ancestor is PID 1. Portable via `psutil` or `/proc` on Linux; `libproc` bindings on macOS. Heavier implementation, worth pricing.
- Teach `uv run` upstream to exit when its own parent dies (likely a non-starter — that's a general tool, not our call).
- Add an idle-timeout in `serve`: no MCP request in N minutes → exit. Cleanest for the Claude-holds-pipes-open failure mode we actually observed.
