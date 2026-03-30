# Agents Guide

Project conventions and decisions for AI coding agents working on this codebase.

## Pre-Merge Live MCP Smoke Test

Before merging any PR that touches retrieval, tool handlers, index backends,
or types, reconnect the MCP server and run these queries against the live
local index. Unit tests mock the retrieval layer and cannot catch page
assembly, filter semantics, schema issues, or stale tool metadata that only
surface against real indexed data.

1. `get_hub_status()` — returns a non-empty index and a recent
   `last_refresh_at`, so smoke-test failures are not caused by a stale or empty
   local corpus
2. `get_doc(path="/server/frames/system-frames")` — returns full multi-chunk
   page (not a single 500-char chunk), confidence 1.0
3. `get_doc(path="/server/frames/system-frames", section="StartFrame")` —
   returns only the StartFrame section from the assembled page
4. `get_doc(doc_id=<id from a search_docs result>)` — returns non-empty content
   and is not `Not Found`
5. `get_doc(path="")` and `get_doc(doc_id="")` — both raise validation errors
6. `get_doc(doc_id="", path="/server/frames/system-frames")` — falls back to
   the path lookup and returns the assembled page
7. `search_api("send_dtmf", class_name="DailyTransport")` — returns
   `DailyTransportClient.send_dtmf` (prefix match)
8. `search_examples("TTS pipeline", domain="backend")` — returns hits with
   backend-style example paths, not unrelated frontend/client files
9. `search_docs("TTS + STT")` — multi-concept returns hits for both concepts
10. `list_tools()` — `get_doc` mentions path lookup, and `get_code_snippet` /
    `search_api` describe `class_name` as a prefix match and list
    `type_definition` in chunk_type
11. `search_api("DialoutSendDtmfSettings", chunk_type="type_definition")` —
    returns the Daily SDK dict schema with field keys
12. `search_api("send_dtmf settings")` — returns method signatures.
    Note: `DialoutSendDtmfSettings` type_definition does not yet surface
    in mixed queries via embedding similarity alone. Use
    `chunk_type="type_definition"` for direct lookup.
13. `get_code_snippet(symbol="CallClient.send_dtmf")` — returns method
    signature with `related_type_defs: ["DialoutSendDtmfSettings"]` linking
    to the dict schema
14. `search_api("PipecatClient")` — returns TS hits from
    `pipecat-ai/pipecat-client-web` (not only Python module overviews)
15. `search_api("WebSocketTransport")` — returns TS class extending
    `Transport` from `pipecat-ai/pipecat-client-web-transports`
16. `search_api("RTVIEvent")` — returns TS type/enum from
    `pipecat-ai/pipecat-client-web`
17. `search_api("VoiceVisualizer React component typescript")` — returns TS
    React component from `pipecat-ai/voice-ui-kit` or `pipecat-ai/pipecat-client-web`
18. `search_api("PipecatClientOptions")` — returns TS interface from
    `pipecat-ai/pipecat-client-web` with `language="typescript"` metadata
19. `search_api("SmallWebRTCTransport")` — returns TS hits from
    `pipecat-ai/pipecat-client-web-transports` or `pipecat-ai/voice-ui-kit`
20. `search_docs("pipecat-client-ios")` — returns at least one hit from an
    iOS SDK repo (README fallback for zero-code-chunk repos)
21. `search_api("PipecatClientProvider")` — returns TS const export from
    `pipecat-ai/pipecat-client-web` with full arrow-function body (not
    truncated at the parameter list)
22. `search_api("SmallWebRTCTransport", class_name="SmallWebRTCTransport")` —
    returns TS class from `pipecat-ai/pipecat-client-web-transports` (verifies
    nested-package TS detection for `small-webrtc-prebuilt`)

If any of these fail, investigate before merging — the unit test suite will
not catch the regression.

## Pre-Merge Quality Gate

Run the full CI gate locally before merging any PR. Do not rely on tests
alone — mypy and ruff catch issues that only surface in CI.

```bash
uv run ruff check src/ tests/
uv run mypy src/ tests/
uv run pytest tests/ -q
```

## Review Checklist

Findings that have been reviewed and deliberately accepted. Do not re-flag these
in future reviews unless the underlying circumstances change.

- **[Architecture] won't-fix**: CodeSnippet enrichment fields (`dependency_notes`, `companion_snippets`, `interface_expectations`) use different names than ApiHit's raw fields (`imports`, `calls`, `yields`, `base_classes`). This is intentional — ApiHit is a raw API surface for exploration, CodeSnippet is an agent-facing enriched view with qualified names and human-readable formatting. Revisit if a third tool type needs the same data. (2026-03-22)

- **[Architecture] won't-fix**: `get_code_snippet` enrichment logic (line_sliced detection, module_overview guard, metadata mapping) is inline in the method rather than extracted into helpers. The method is ~50 lines with clear comments. Extract helpers if enrichment gains more suppression conditions or new enrichment fields. (2026-03-22)

- **[Security] won't-fix**: Chunk metadata values (class_name, calls, yields, etc.) flow unsanitized into MCP JSON-RPC responses. The AST ingester constrains these to valid Python identifiers; the TS regex parser extracts names from cloned GitHub repo source (not user input). No executable sink exists. Add input validation if user-supplied metadata or external API sources are introduced. (2026-03-22, updated 2026-03-30)

- **[Architecture] won't-fix**: `ApiHit.imports` has mixed precision by chunk type — per-method for method/function chunks, module-level pipecat imports for class_overview, full imports (including stdlib) for module_overview. This is a deliberate layering: `source_ingest._build_chunks` populates each chunk type differently, and `hybrid.py` passes the field through unchanged. The `ApiHit.imports` description documents the per-chunk-type semantics. Revisit only if a consumer needs uniform precision across chunk types. (2026-03-23)

- **[Logic] won't-fix**: Confidence scores are optimistic on weak `search_examples` results — noisy keyword matches from large repos (e.g., gradient-bang frontend files) score high via RRF + dual-hit bonus, driving confidence to ~0.95 even when results are semantically irrelevant. This is a retrieval quality issue, not a confidence calibration bug. The cross-encoder (Phase 1, disabled by default) directly addresses this by scoring query-result *pairs* for semantic relevance. Without cross-encoder, confidence reflects score distribution, not true relevance. Follow-up: example corpus weighting / repo scoring to reduce noise from non-pipeline code. (2026-03-24)

- **[Security] accepted-risk**: `pip-audit` reports `pygments 2.19.2` for `CVE-2026-4539`, but as of March 25, 2026 it does not provide a fixed PyPI version. The package is currently present transitively via `rich`/`pytest`, so the repo-local audit gate ignores this single CVE with an explicit `--ignore-vuln` entry rather than disabling `pip-audit` more broadly. Revisit as soon as upstream publishes a fixed release or the advisory guidance changes. (2026-03-25)
