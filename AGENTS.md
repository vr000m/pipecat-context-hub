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
2. `get_doc(path="/api-reference/server/frames/system-frames")` — returns
   full multi-chunk page (not a single 500-char chunk), confidence 1.0
3. `get_doc(path="/api-reference/server/frames/system-frames", section="StartFrame")`
   — returns only the StartFrame section from the assembled page
4. `get_doc(doc_id=<id from a search_docs result>)` — returns non-empty content
   and is not `Not Found`
5. `get_doc(path="")` and `get_doc(doc_id="")` — both raise validation errors
6. `get_doc(doc_id="", path="/api-reference/server/frames/system-frames")` —
   falls back to the path lookup and returns the assembled page
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
    React component from `pipecat-ai/voice-ui-kit` or `pipecat-ai/pipecat-client-web`.
    Also try bare `search_api("VoiceVisualizer")` — currently requires the
    qualifier to rank above Python hits, but should improve as retrieval
    quality improves (cross-encoder, corpus weighting). If the bare query
    starts passing, that's a positive signal.
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
23. `search_api("connect", class_name="PipecatClient")` — returns TS method
    chunk with `method_signature` from `pipecat-ai/pipecat-client-web`
    (Phase 2 tree-sitter method extraction)
24. `search_api("initialize", class_name="Transport")` — returns TS abstract
    method from `pipecat-ai/pipecat-client-web` (verifies abstract method
    extraction and no _MIN_METHOD_LINES filtering)
25. `search_api("WebSocketTransport", chunk_type="class_overview")` — returns
    the TS class declaration (not just method chunks). Verifies class-level
    chunks still rank when method extraction adds many per-class hits.
26. `search_api("PipecatClientOptions", chunk_type="class_overview")` —
    returns the TS interface declaration from `pipecat-ai/pipecat-client-web`.
    Same ranking-stability check as test 25.
27. `get_code_snippet(symbol="PipecatClient.connect")` — returns the TS
    method snippet with full `method_signature` (end-to-end symbol lookup
    for TS method chunks, not just search_api ranking)
28. `search_api("connected", class_name="PipecatClient")` — returns the TS
    getter chunk from `pipecat-ai/pipecat-client-web` (verifies getter
    extraction — a separate code path from regular methods)
29. `search_api("constructor", class_name="PipecatClient")` — returns the
    constructor chunk with full signature `(options: PipecatClientOptions)`

Note on bare TS symbol queries (e.g., `search_api("WebSocketTransport")`
without `chunk_type` or `class_name` filters): after Phase 2 method
extraction, method/getter chunks may rank ahead of the class declaration.
This is expected — don't treat "class is not top result" as a hard blocker.
Use `chunk_type="class_overview"` (tests 25-26) when class-level ranking
matters.

30. `search_examples("TTS pipeline", pipecat_version="0.0.95", domain="backend")`
    — all hits have `version_compatibility: "newer_required"` (framework pins
    are 0.0.108+)
31. `search_examples("TTS pipeline", pipecat_version="0.0.110", domain="backend")`
    — all hits have `version_compatibility: "compatible"`
32. `search_examples("TTS pipeline", pipecat_version="0.0.110",
    version_filter="compatible_only", domain="backend")` — no
    `newer_required` hits pass through the filter
33. `search_examples("TTS pipeline")` (no version) — all hits have
    `version_compatibility: null`
**Prerequisite:** Tests 34-37 require that `gh` CLI was authenticated during
the last `refresh`. Without `gh`, release-note-derived deprecation entries
will be absent and these assertions will fail. Test 36 (`DailyTransport`)
always passes regardless of `gh` availability.

34. `check_deprecation("pipecat.services.grok.llm")` — returns
    `deprecated: true`, `deprecated_in: "0.0.108"`, replacement includes
    `pipecat.services.xai.llm`, note includes PR link
35. `check_deprecation("SambaNovaSTTService")` — returns `deprecated: true`,
    `removed_in: "0.0.108"`
36. `check_deprecation("DailyTransport")` — returns `deprecated: false`
37. `check_deprecation("pipecat.services.google.llm_vertex")` — returns
    `deprecated: true`, `deprecated_in: "0.0.105"`, replacement includes
    `pipecat.services.google.vertex.llm`
38. `get_hub_status()` after `refresh --framework-version v0.0.96` — response
    includes `framework_version: "v0.0.96"` (confirms pinned version persisted
    and surfaced)
39. `refresh --framework-version nonexistent-tag-xyz` — fails with a clear
    `ValueError` mentioning "not found" and listing available tags (confirms
    tag validation rejects invalid input)
40. `uv run pipecat-context-hub serve` — startup `INFO` log line
    `pipecat-context-hub vX.Y.Z starting: data_dir=<path> total=N counts_by_type={code=N,doc=N,source=N}`
    appears with non-zero `total` (confirms version banner, index-populated
    state, and content-type counts are observable from the MCP trace)
41. `PIPECAT_HUB_RERANKER_ENABLED=0 uv run pipecat-context-hub serve` — startup
    `WARNING` log line `Reranker disabled at startup: reason=config_disabled
    configured_model=…` appears. Then re-run with
    `PIPECAT_HUB_RERANKER_MODEL=cross-encoder/does-not-exist` — the warning
    reports `reason=not_cached` and the remediation hint includes the HF cache
    path that was probed (e.g. `checked HF cache: /…/huggingface/hub`)

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

- **[Security] won't-fix**: Chunk metadata values (class_name, calls, yields, etc.) flow unsanitized into MCP JSON-RPC responses. The AST ingester constrains these to valid Python identifiers; the TS tree-sitter parser extracts names from cloned GitHub repo source (not user input). No executable sink exists. Add input validation if user-supplied metadata or external API sources are introduced. (2026-03-22, updated 2026-03-30)

- **[Architecture] won't-fix**: `ApiHit.imports` has mixed precision by chunk type — per-method for method/function chunks, module-level pipecat imports for class_overview, full imports (including stdlib) for module_overview. This is a deliberate layering: `source_ingest._build_chunks` populates each chunk type differently, and `hybrid.py` passes the field through unchanged. The `ApiHit.imports` description documents the per-chunk-type semantics. Revisit only if a consumer needs uniform precision across chunk types. (2026-03-23)

- **[Logic] won't-fix**: Confidence scores are optimistic on weak `search_examples` results — noisy keyword matches from large repos (e.g., gradient-bang frontend files) score high via RRF + dual-hit bonus, driving confidence to ~0.95 even when results are semantically irrelevant. This is a retrieval quality issue, not a confidence calibration bug. The cross-encoder (Phase 1, disabled by default) directly addresses this by scoring query-result *pairs* for semantic relevance. Without cross-encoder, confidence reflects score distribution, not true relevance. Follow-up: example corpus weighting / repo scoring to reduce noise from non-pipeline code. (2026-03-24)

- **[Security] resolved**: `pygments` CVE-2026-4539 resolved by upgrading to 2.20.0 via PR #34 (2026-03-31). `--ignore-vuln` entry removed from CI and justfile.

- **[Security] resolved**: `lxml` GHSA-vfmq-68hx-4jfw / CVE-2026-41066 (XXE via default `iterparse()` / `ETCompatXMLParser()` config) resolved by pinning `lxml>=6.1.0` in the dev group and bumping `cyclonedx-bom` from `>=4.1,<5.0` to `>=7.3,<8.0` (cyclonedx-bom 4.x transitively pinned `lxml<6`). Landed via PR #50 (2026-04-22).

- **[Security] won't-fix**: `transformers` CVE-2026-1839 — fix requires 5.0.0rc3 (release candidate), but `sentence-transformers` pins `transformers<5.0`. Ignored via `--ignore-vuln CVE-2026-1839` in CI and justfile. Remove when `sentence-transformers` supports `transformers>=5.0`. (2026-04-07)

- **[Architecture] won't-fix**: Removing `pipecat_context_hub.services.ingest.ts_source_parser` is intentional. The module is treated as internal implementation detail, not supported public API, and no external consumers are expected to import it directly. Revisit only if ingestion parser modules become documented extension points. (2026-03-30)

- **[Security] won't-fix**: TypeScript import metadata currently stores raw `import_statement` text from indexed repos. This matches the existing model where source-derived metadata is returned verbatim and no executable sink exists. Revisit if user-supplied repos or prompt-sensitive metadata consumers are introduced. (2026-03-30)

- **[Architecture] won't-fix**: The TypeScript parser-to-chunk contract is intentionally direct for Phase 2: `ts_tree_sitter_parser.py` emits the declaration/member fields that `source_ingest._build_ts_chunks` needs, mirroring the current Python `_build_chunks` pattern. Extract a normalization layer only if later language phases need a shared intermediate representation. (2026-03-30)

- **[Security] won't-fix**: `DeprecationEntry.note` stores raw release-note prose from `pipecat-ai/pipecat` and returns it verbatim via `check_deprecation`. The source is the trusted upstream framework repo (not user input), and MCP JSON-RPC has no executable sink. Revisit if user-supplied repos are introduced as deprecation sources. (2026-04-07)

- **[Architecture] won't-fix**: `_fetch_release_notes()` shells out to `gh` directly from `deprecation_map.py` rather than going through an adapter in the orchestration layer. The function already handles missing CLI, auth failures, and timeouts gracefully with warning-level logging. Extract to a dedicated adapter only if other modules need GitHub release data. (2026-04-07)

- **[Logic] won't-fix**: Multi-item replacement paths from release notes are collapsed into a single comma-joined string assigned to all deprecated paths in the same bullet. This is informational metadata — users see all possible replacements rather than a potentially incorrect positional guess. Improve to positional pairing only if release notes adopt a consistent 1:1 format. (2026-04-07)

- **[Logic] won't-fix**: `DeprecationMap.check()` reverse-prefix matching (`pipecat.services` matches `pipecat.services.grok`) returns the first matching entry, which may be arbitrary when multiple children exist. This is documented behavior for broad queries. Callers should use specific module paths for precise results. (2026-04-07)

- **[Architecture] won't-fix**: Release-note entries do not override an existing `new_path` from source-derived mappings. Source-parsed `DeprecatedModuleProxy` mappings are module-to-module precise, while release notes may list multiple replacement paths. Keeping source-derived `new_path` as authoritative preserves precision. Revisit if source parsing is fully removed. (2026-04-07)
