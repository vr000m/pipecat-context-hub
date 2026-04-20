# Task: Configurable Cross-Encoder Reranker Model Selection

**Status**: Complete (v0.0.17)
**Assigned to**: vr000m
**Priority**: Low
**Branch**: `feature/reranker-model-selection`
**Created**: 2026-04-20
**Completed**: 2026-04-20

## Objective

Let users select between the three allowlisted cross-encoder reranker models via an environment variable (`PIPECAT_HUB_RERANKER_MODEL`), without editing Python config. Surface the active model in `get_hub_status` so users can verify which model is running.

## Context

The hub ships with `cross-encoder/ms-marco-MiniLM-L-6-v2` (~80 MB) as the default reranker. `CrossEncoderReranker` already allowlists three models with very different download and runtime costs (`src/pipecat_context_hub/services/retrieval/cross_encoder.py:24-28`), but the choice is only reachable from Python — there is no CLI flag or env var. Today:

- `PIPECAT_HUB_RERANKER_ENABLED=0` can disable reranking entirely — a blunt toggle that sacrifices result quality.
- Users on slow or throttled HuggingFace Hub connections have no path to a smaller model short of patching the code.
- `get_hub_status` reports `server_version` and `framework_version` but says nothing about the active reranker, so users and agents cannot confirm configuration took effect without reading server logs.

Giving users an env-var knob lets them trade quality for download size when needed. Surfacing the active model in `get_hub_status` closes the diagnostic loop — one tool call shows which model is live.

## Requirements

- `PIPECAT_HUB_RERANKER_MODEL` env var selects the reranker model by name.
- Accepted values: the three entries in `_ALLOWED_MODELS` (MiniLM-L-6-v2, MiniLM-L-12-v2, TinyBERT-L-2-v2).
- Invalid values emit a warning and fall back to the configured default — **never raise**, to keep the server bootable in misconfigured environments.
- Precedence: env var > `RerankerConfig.cross_encoder_model` field > hardcoded default.
- `get_hub_status` returns `reranker_enabled: bool` (effective, after env override) and `reranker_model: str | None` (None when disabled).
- Per-query search responses (`search_docs`, `search_examples`, etc.) do **not** include reranker info — token bloat for data that doesn't change per-call.
- Backwards compatible: existing `PIPECAT_HUB_RERANKER_ENABLED` behaviour unchanged; default model unchanged.
- CLAUDE.md documents the three models, their sizes/latencies, and how to swap.

## Review Focus

- **Env var validation path**: invalid model name must log a warning and fall back — not raise. Check both "unknown string" and "empty string" cases.
- **Allowlist coupling**: `_ALLOWED_MODELS` lives in `cross_encoder.py`; the config resolver in `shared/config.py` must not duplicate the list. Either import the allowlist or expose a validator helper to keep one source of truth.
- **HubStatusOutput shape change**: adding fields to a Pydantic model surfaced by an MCP tool. Verify no client code consumes the model with `extra="forbid"` semantics and that the new fields default to safe values when reranker is disabled.
- **Env var consumption point**: resolve the env var in `RerankerConfig` (colocated with `effective_enabled`) rather than at CLI startup, so every construction path sees it consistently.

## Implementation Checklist

### Phase 1: Config plumbing
- [x] Add `_RERANKER_MODEL_ENV = "PIPECAT_HUB_RERANKER_MODEL"` constant in `shared/config.py`.
- [x] Add `effective_model` computed property on `RerankerConfig` that resolves env var → field → default, validates against `_ALLOWED_MODELS`, warns + falls back on invalid.
- [x] Import `_ALLOWED_MODELS` from `services/retrieval/cross_encoder.py` into `shared/config.py` (single source of truth) — or expose a small `is_allowed_reranker_model()` helper if circular-import trouble arises.
- [x] Update `cli.py:114` and `cli.py:210` to read `config.reranker.effective_model` instead of `cross_encoder_model`.

### Phase 2: Status tool
- [x] Extend `HubStatusOutput` (`shared/types.py`) with `reranker_enabled: bool` and `reranker_model: str | None` fields.
- [x] Populate them in `server/tools/get_hub_status.py` from `config.reranker.effective_enabled` and `config.reranker.effective_model`.
- [x] Thread `AppConfig` (or a reranker snapshot) into the `get_hub_status` handler — it currently only takes `IndexStore`.

### Phase 3: Tests
- [x] Unit: `RerankerConfig.effective_model` — each allowed value via env, invalid value, empty string, unset.
- [x] Unit: `HubStatusOutput` includes new fields; `reranker_model` is None when disabled.
- [x] Integration: `get_hub_status` end-to-end with each of the three models set via env (mock sentence-transformers to avoid actual download).

### Phase 4: Docs + release
- [x] Update CLAUDE.md "Cross-Encoder Reranking" section with the model table + env var.
- [x] Add a note on pre-warming: run `uv run pipecat-context-hub refresh` once to download the reranker before first MCP query.
- [x] CHANGELOG.md entry under `Added`.
- [x] Bump `_SERVER_VERSION` and `pyproject.toml` version (both places — enforced by `TestVersionConsistency`).
- [x] PR → `/review` → `/security-review` → `/deep-review` → merge → `gh release create`.

## Technical Specifications

### Files to Modify

- `src/pipecat_context_hub/shared/config.py` — add env var constant, `effective_model` computed property on `RerankerConfig`.
- `src/pipecat_context_hub/shared/types.py` — add two fields to `HubStatusOutput`.
- `src/pipecat_context_hub/cli.py` — read `effective_model` at lines 114 and 210.
- `src/pipecat_context_hub/server/tools/get_hub_status.py` — accept config/reranker info, populate new fields.
- `src/pipecat_context_hub/server/main.py` — thread reranker state into the `get_hub_status` handler (dependency injection tweak) and bump `_SERVER_VERSION`.
- `tests/unit/test_config.py` — env var resolution tests.
- `tests/unit/test_server.py` — `get_hub_status` payload tests.
- `CLAUDE.md` — reranker table + swap instructions.
- `CHANGELOG.md` — `Added` entry.
- `pyproject.toml` — version bump.

### New Files to Create

None expected.

### Architecture Decisions

- **Env var only, no CLI flag.** MCP servers are launched from JSON config (Claude Code `.mcp.json`, Cursor config, etc.), not interactively. Users already set `PIPECAT_HUB_RERANKER_ENABLED` via the `env` block in those configs; `PIPECAT_HUB_RERANKER_MODEL` follows the same pattern with zero new surface area. If a `--reranker-model` flag is later requested, it is additive.
- **Validate at config layer, not at reranker layer.** `CrossEncoderReranker.__init__` already warns + disables on disallowed names. Duplicating the warning at config layer is fine because the config-layer fallback keeps the system enabled on the default model instead of silently disabling.
- **Status metadata, not per-query metadata.** Matches `server_version` / `framework_version` pattern. Keeps search responses lean.
- **Don't touch search tool outputs.** Zero risk to existing integrations.

### Dependencies

No new runtime dependencies. Tests may need a sentence-transformers mock if not already present.

### Integration Seams

| Seam | Writer (task) | Caller (task) | Contract |
|------|---------------|---------------|----------|
| `_ALLOWED_MODELS` allowlist | `services/retrieval/cross_encoder.py` | `shared/config.py` (new env-var validator) | Single source of truth — config imports, does not redeclare |
| `RerankerConfig.effective_model` | `shared/config.py` | `cli.py` (server + refresh paths), `get_hub_status` handler | Always returns a string in `_ALLOWED_MODELS`; never raises |
| `HubStatusOutput` new fields | `shared/types.py` | `get_hub_status` tool, downstream MCP clients | `reranker_model` is `None` iff `reranker_enabled is False` |

## Testing Notes

### Test Approach

- [x] Unit tests for `RerankerConfig.effective_model` covering: unset env, each valid value, invalid string, empty string, case-sensitivity.
- [x] Unit tests for `HubStatusOutput` shape (field presence + None-when-disabled invariant).
- [x] Integration test for `get_hub_status` with each allowed model set via env (monkeypatched), asserting the active model is returned.
- [x] Manual: run `uv run pipecat-context-hub serve` with `PIPECAT_HUB_RERANKER_MODEL=cross-encoder/ms-marco-TinyBERT-L-2-v2`, call `get_hub_status`, confirm the field.

### Test Results

- [x] All existing tests pass (`uv run pytest`).
- [x] New tests added and passing.
- [x] Manual verification complete.

### Edge Cases Tested

- [x] Env var set but reranker disabled → `reranker_model` is `None`, no warning.
- [x] Invalid env var value → warning logged, default model used, server starts.
- [x] Env var set to the default value → behaves identically to unset.

## Acceptance Criteria

- [x] `PIPECAT_HUB_RERANKER_MODEL` selects the reranker model across all three allowed values.
- [x] Invalid values fall back to default with a warning — server does not crash.
- [x] `get_hub_status` returns `reranker_enabled` and `reranker_model` fields.
- [x] `reranker_model` is `None` when reranker is disabled.
- [x] CLAUDE.md documents the three models + env var.
- [x] CHANGELOG.md entry added under `Added`.
- [x] All tests pass.
- [x] `/review`, `/security-review`, `/deep-review` clean.
- [x] Version bumped in both locations (`pyproject.toml` + `_SERVER_VERSION`).

## Open Questions

1. **CLI flag on `serve`?** Current lean: env-var-only. If a user strongly wants `--reranker-model`, it is additive. Defer unless requested.
2. **Surface the reranker info in tool responses too?** Leaning no (token bloat). Revisit if users report they cannot tell which model served a given query during latency debugging.

## Final Results

### Summary

Added `PIPECAT_HUB_RERANKER_MODEL` env var to select one of three
allowlisted cross-encoder models without editing Python config. Extended
`get_hub_status` with live runtime state (`reranker_enabled`,
`reranker_model`, `reranker_configured_model`, `reranker_disabled_reason`)
so operators can diagnose degraded reranking from a single MCP tool call.
Bundled into the v0.0.17 release alongside the Windows refresh fixes.

### Outcomes

- Env-var validation path never raises — invalid values log a warning and
  fall back to the field-or-default model; server always boots.
- `_ALLOWED_RERANKER_MODELS` lives in `shared/config.py` as the single
  source of truth; `cross_encoder.py` imports it upward (no dependency
  inversion).
- `effective_model` is a pure computed property (no side effects); all
  configuration warnings fire exactly once via a `model_validator` at
  construction time.
- `get_hub_status` surfaces the operator's *raw* requested model in
  `reranker_configured_model`, so a typo'd env var is visible in the
  tool output without reading server logs.
- `disabled_reason` is typed `Literal["config_disabled" | "not_cached" |
  "load_failed"] | None` for mypy/Pydantic enforcement.
- Two rounds of adversarial review (Codex + multi-lens `/deep-review`)
  surfaced real issues — (a) status reporting config intent instead of
  live state, (b) fallback-target misreported in warnings, (c)
  `configured_model` masking misconfigurations — all fixed.

### Learnings

- Operators need a *diagnostic* view, not just a *configured* view:
  threading a live-state callable through `create_server` proved more
  valuable than a static snapshot.
- Side-effecting `computed_field` properties are easy to add and hard to
  spot; `model_validator(mode="after")` is the right hook for one-time
  validation + logging.
- Pin `Literal` types for sentinel strings the moment they appear —
  pipe-delimited docstrings rot; typed aliases don't.

### Follow-up Work

- Consider eager-loading the reranker at `serve` startup so download
  failures surface at boot instead of on first query. (The MCP-stdio
  startup budget makes this non-trivial; revisit if `load_failed` shows
  up in real operator reports.)
