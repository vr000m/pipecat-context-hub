# Pipecat Context Hub Architecture Plan

## Header
- **Status:** In Progress (v0.0.9)
- **Type:** design
- **Assignee:** vr000m
- **Priority:** High
- **Working Branch:** main (individual features on topic branches; see Completed Tasks in README.md)
- **Created:** 2026-02-18
- **Target Completion:** 2026-03-06
- **Objective:** Design and implement a local-first MCP platform that provides fresh Pipecat docs/examples context for Claude Code, Cursor, VS Code, and Zed.

## Release Scope
### v0 MVP (target: 2026-03-06)
- Retrieval-first local MCP server over `stdio`.
- Core tools only: `search_docs`, `get_doc`, `search_examples`, `get_example`, `get_code_snippet`.
- Local index refresh workflow (`refresh` command), no hosted ops stack.
- Sources: Pipecat docs + `pipecat/examples` (including `examples/foundational`) + `pipecat-examples`.

### v1 Follow-up (post-MVP)
- Higher-order tools: `compose_solution`, `propose_architecture`.
- ~~More advanced reranking and guardrail inference.~~ → See item 7 below.
- Optional scheduled auto-refresh and richer local observability.
- Decide and document refresh failure policy: **empty-on-failure** (current v0 behavior — stale data is worse than missing data for LLM context) vs **retain-previous-on-failure** (keep last-known-good records when ingestion fails). May require snapshot/swap semantics in IndexStore.
- Version-pinned ingestion: allow pinning to a specific pipecat release tag instead of always ingesting HEAD. Track index-level metadata (pipecat version, docs fetch timestamp) so users building against older pipecat versions get matching context. Warn when the indexed pipecat version diverges from the user's installed version.
- **Call-graph metadata for dependency tracing** (requirement #9 gap): The biggest
  reason agents fall back to `.venv` reads is tracing call chains — "method A calls
  method B which yields frame C." Current chunks are isolated definitions with no
  links between them. Scope:
  1. ~~**Extract yield types** from method bodies~~ ✅ Done (`ast_extractor._extract_yields`,
     `_walk_body_shallow` for scope boundary). Stored as `yields: [...]` on
     method/function chunks. Only `ast.Yield` (not `YieldFrom` — generator
     delegation names aren't frame types). Walks only executable body,
     excluding decorators, defaults, annotations, nested defs, and lambdas.
  2. ~~**Extract method calls** from method bodies~~ ✅ Done (`ast_extractor._extract_calls`).
     Patterns: `self.method()` → `"method"`, `ClassName.method()` → `"ClassName.method"`,
     `super().method()` → `"super().method"`. Lowercase attribute chains excluded.
  3. ~~**Propagate imports to class/method chunks**~~ ✅ Done (`source_ingest._build_chunks`).
     Pipecat-internal imports (absolute + relative) propagated to class_overview
     and method chunks. Relative import dots preserved via `node.level` in
     `_extract_imports`. Module overview retains full imports list.
  4. ~~**Make filterable**~~ ✅ Done. FTS: `_build_filter_sql()` with JSON-key-anchored
     LIKE patterns. Vector: `_apply_post_filters()` with list membership checks
     (post-filter, not push-down — yields/calls are JSON strings in ChromaDB).
     `SearchApiInput` exposes `yields` and `calls` filter params; `ApiHit` includes
     both as structured list fields.
  5. ~~**Populate `dependency_notes` and `companion_snippets`**~~ ✅ Done. Retrieval-time
     enrichment in `hybrid.py` — no index changes needed. `get_code_snippet` now maps
     chunk metadata to all three `CodeSnippet` enrichment fields:
     - `dependency_notes` ← `imports` (module-level pipecat imports, not yet
       per-method — follow-up: extract per-method imports from AST)
     - `companion_snippets` ← `calls` (qualified with `class_name` prefix)
     - `interface_expectations` ← `yields` + `base_classes` (human-readable strings)
     Handles both JSON-string and native-list metadata formats. Field description for
     `companion_snippets` updated from "IDs of related snippets" to "Qualified method
     names called by this snippet." Tests in `test_retrieval.py` (4 cases) and
     `test_mcp_tools.py` (yields/calls filter validation).
  6. ~~**Per-method import extraction for `dependency_notes`**~~ ✅ Done. `dependency_notes`
     is currently empty because chunk metadata stores module-level `pipecat_imports`
     (every method in a file gets the same list). Branch: `feature/per-method-imports`.
     - **Approach:** Walk each method/function body with `_walk_body_shallow`,
       collect `ast.Name.id` references, cross-reference against a name map built
       from `module_info.imports`, store only the matched pipecat-internal subset.
     - **Two-pass extraction in `extract_module_info`:** Imports, classes, and
       functions are currently extracted in a single source-order loop. Since a
       class can appear before an import in source, the name map must be built
       first. Solution: pass 1 collects all `ast.Import`/`ast.ImportFrom` nodes;
       pass 2 extracts classes/functions with the completed name map.
     - **AST changes (`ast_extractor.py`):**
       - Add `_build_import_name_map(raw_imports: list[ast.Import | ast.ImportFrom]) -> dict[str, str]`
         — builds mapping from AST nodes (not parsed strings) to handle aliases
         correctly. Maps `{bare_name_or_alias: full_import_string}`. For
         `from X import Y as Z` → `{"Z": "from X import Y as Z"}`. For
         multi-name `from X import A, B` → `{"A": "from X import A, B",
         "B": "from X import A, B"}`. For `import X.Y.Z` → `{"X": "import X.Y.Z"}`
         (leftmost component only, matching `ast.Name.id` resolution).
       - Add `_extract_used_imports(node, name_map, pipecat_filter) -> list[str]`
         — walks body with `_walk_body_shallow`, collects `ast.Name.id` refs,
         cross-references against name map, returns only pipecat-internal matches
         (deduplicated, order of first use).
       - Add `imports: list[str]` field to `MethodInfo` and `FunctionInfo`
       - Thread name map: `extract_module_info` builds it after pass 1, passes
         to `_extract_class(node, source_lines, name_map)` →
         `_extract_method(node, source_lines, name_map)` and to
         `_extract_function(node, source_lines, name_map)`.
       - Fix `_extract_imports` to preserve aliases: use `alias.asname or alias.name`
         in the output string so `from X import Y as Z` produces
         `"from X import Y as Z"` (currently drops `asname`).
     - **Ingestion changes (`source_ingest.py`):** method/function chunks use
       `method.imports` / `func.imports` instead of `pipecat_imports`. Class
       overview keeps module-level `pipecat_imports`. Module overview keeps full list.
     - **Retrieval changes (`hybrid.py`):** re-enable `dependency_notes` from
       `_parse_metadata_list(r.chunk.metadata, "imports")` (remove `[]` stub).
       Same suppression rules as other enrichment fields (skip when `line_sliced`
       or `chunk_type == "module_overview"`).
     - **`search_api` behavior change:** `ApiHit.imports` will shift from
       module-level to per-method for method/function chunks. This is more
       correct — a method result should show what it needs, not the whole file.
       Update `ApiHit.imports` field description accordingly.
     - **Tests:**
       - `test_ast_extractor.py`: `_build_import_name_map` (multi-name, alias,
         relative, dotted), `_extract_used_imports` (used subset, unused excluded,
         scope boundary respected)
       - `test_source_ingest.py`: update `test_method_chunk_has_pipecat_imports_only`
         — method should now have only imports it actually uses
       - `test_retrieval.py`: update `TestCodeSnippetEnrichment` — re-enable
         `dependency_notes` assertions for non-sliced, non-module_overview cases
         (tests: `test_enrichment_from_metadata`, `test_enrichment_with_native_list_metadata`,
         `test_enrichment_kept_when_path_covers_full_chunk`,
         `test_enrichment_kept_when_path_line_start_with_max_lines`,
         `test_enrichment_kept_when_truncated_by_max_lines`). Tests that assert
         `[]` stay unchanged: `test_enrichment_empty_metadata`,
         `test_enrichment_skipped_when_sliced_by_path_line`,
         `test_enrichment_skipped_when_path_line_start_mid_chunk`,
         `test_enrichment_skipped_for_module_overview`.
       - `test_mcp_tools.py`: no changes (uses mock retriever)
     - **Doc updates:** Update `CodeSnippet.dependency_notes` description in
       `types.py`, `ApiHit.imports` description, `CHANGELOG.md`, dev plan status.
     - **Known limitations (documented, not fixed):**
       - Imports used only in parameter/return type annotations won't be captured
         (`_walk_body_shallow` excludes decorators, defaults, annotations by design).
         Acceptable for v0 — `dependency_notes` targets runtime dependencies.
       - `import X.Y.Z` only matches the leftmost name `X` via `ast.Name.id`.
         Full dotted `ast.Attribute` chain resolution is out of scope.
  - **Non-goal:** Full type-resolved call graph. Name-based extraction is sufficient
    for the retrieval use case and avoids the complexity of cross-module type
    inference.
  - **Evidence:** Agent session logs show repeated `.venv` reads for
    `BaseTransport`, `FrameProcessor`, and `PipelineTask` — all cases where the
    agent found the class definition but couldn't trace what it calls or yields.
  7. **Advanced reranking & retrieval quality** — Branch: `feature/advanced-reranking`.
     Current retrieval uses RRF (vector + keyword merge) with two heuristics
     (symbol boost +0.15, staleness penalty -0.05). Deep pipeline exploration
     identified 8 quality bottlenecks and 8 unused signals.

     **Phase 1: Cross-encoder reranker** (highest ROI)
     - **Async design:** `rerank()` stays sync (all existing tests unchanged).
       New `CrossEncoderReranker` service class owns model lifecycle and
       thread offload. `HybridRetriever` holds an optional instance. Call
       site in `_single_concept_search`: sync `rerank()` → async
       `await cross_encoder.rerank(candidates, query)` → `[:limit]`.
     - `CrossEncoderReranker` (new file `services/retrieval/cross_encoder.py`):
       - `__init__(config: RerankerConfig)` — stores config, model=None
       - `async def rerank(candidates, query, top_n) -> list[IndexResult]` —
         lazy-loads model on first call, runs `asyncio.to_thread(self._score, ...)`
       - `_score(candidates, query)` — sync, calls `CrossEncoder.predict()`
       - `ensure_model()` — pre-download, called from `refresh` CLI when enabled
     - Model: `cross-encoder/ms-marco-MiniLM-L-6-v2` (~22M params, ~80MB download)
     - Config: new `RerankerConfig` in `config.py` with `cross_encoder_model`,
       `enabled` (default `False`), `top_n` (default 20). Add as field on
       `HubConfig`. Plumb through `cli.py` / `server/main.py` →
       `HybridRetriever.__init__`.
     - **Model download / offline policy:**
       - `refresh --force` pre-downloads when enabled (alongside embedding model)
       - `serve` startup: if enabled but model not cached, log warning and
         disable cross-encoder (fall back to RRF-only). No query-time download.
       - Offline: works without cross-encoder. Degraded, not broken.

     **Phase 2: Result diversity**
     - **Repo diversity:** MMR-style penalty for consecutive results from same repo
     - **File diversity:** penalty for results from same file path
     - **Chunk-type preference for `search_api`:** method > class_overview >
       module_overview — **only when `chunk_type` filter is not explicitly set**.
       If the user requests a specific chunk_type, no preference is applied.
     - Implement as `_apply_diversity()` in `rerank.py` (sync, called from
       inside `rerank()` after heuristics). Diversity runs before cross-encoder
       since cross-encoder is async and lives in `hybrid.py`.

     **Phase 3: Confidence-based guardrails**
     - **Low-confidence flag:** add `low_confidence: bool = False` to
       `EvidenceReport` — set `True` when `confidence < 0.3`. Default `False`
       preserves backward compatibility with existing construction sites.
     - **Graduate count contribution:** replace `min(count/10, 0.1)` cap with
       `min(count/15, 0.15)` for high-score tier
     - **Cross-tool suggestions:** derive from `content_type` filter in
       `_generate_next_queries`. When `content_type == "doc"` and 0 results →
       suggest `search_examples`; when `content_type == "source"` and 0 results
       → suggest `search_docs`. Keeps it in `evidence.py` without coupling to
       tool-level concerns.
     - **Confidence floor:** below 0.15 overall confidence, insert an
       `UnknownItem` explaining "The index has no strong matches for this query"

     **Phase 4: Heuristic fixes**
     - **UPPERCASE symbol detection:** extend `_extract_query_symbols` to
       recognize 2+ letter ALL-CAPS tokens (TTS, STT, VAD, RTVI, LLM) as
       code symbols
     - **Dual-hit bonus:** +0.10 when a chunk appears in both vector AND
       keyword results. Detected during RRF fusion (chunk_id appears in
       multiple ranked lists).
     - **Graduated staleness:** replace binary 90-day threshold with linear
       decay: `penalty = min(0.10, age_days / 365 * 0.10)` — gentle ramp,
       max -0.10 at 1 year
     - **BM25/vector dedup clarification:** the current raw-score comparison
       in dedup (`rerank.py:183`) has no meaningful impact on final ordering
       since `apply_code_intent_heuristics` overwrites `result.score` with
       the RRF-derived score. Change to use RRF scores for the dedup winner
       selection for consistency, but note this is a cleanup, not a behavior fix.
     - **Event loop fix (read path):** wrap the inner sync calls inside
       `IndexStore.vector_search` (`self._vector.search(query)`) and
       `IndexStore.keyword_search` (`self._fts.search(query)`) with
       `asyncio.to_thread`. No changes to method signatures or the
       `IndexReader` protocol — the wrapping is internal to `store.py`.
       Write-path methods (`upsert`, `delete_*`) are explicitly deferred —
       they run during `refresh` (CLI, blocking by design).

     **Config plumbing:**
     1. Add `RerankerConfig` to `config.py` (model, enabled, top_n)
     2. Add `reranker: RerankerConfig` field to `HubConfig`
     3. `cli.py` → pass config to `CrossEncoderReranker` + `HybridRetriever`
     4. `HybridRetriever.__init__` accepts optional `CrossEncoderReranker`
     5. `_single_concept_search`: sync `rerank()` → async cross-encoder → limit

     **Files to modify:**
     | File | Changes |
     |------|---------|
     | `services/retrieval/cross_encoder.py` | **New file.** `CrossEncoderReranker` service class |
     | `services/retrieval/rerank.py` | Diversity stage, all heuristic fixes (stays sync) |
     | `services/retrieval/hybrid.py` | Optional `CrossEncoderReranker`, async call after `rerank()` |
     | `services/retrieval/evidence.py` | Guardrail improvements, confidence formula, cross-tool suggestions |
     | `shared/config.py` | `RerankerConfig`, add to `HubConfig` |
     | `shared/types.py` | `low_confidence: bool = False` on `EvidenceReport` |
     | `services/index/store.py` | `asyncio.to_thread` wrapping inside read methods |
     | `cli.py` | Plumb reranker config, pre-download model on refresh |
     | `server/main.py` | Pass `CrossEncoderReranker` to `HybridRetriever` |
     | `docs/README.md` | Update reranking description in Technology section |
     | `CLAUDE.md` | Note cross-encoder config option |

     **Testing plan (per-phase):**
     - **Phase 1 tests:** `CrossEncoderReranker` with mock model (enabled/disabled),
       lazy loading, thread offload, offline fallback. Regression: disabled →
       output identical to current `rerank()` pipeline.
     - **Phase 2 tests:** `_apply_diversity()` with repo/file/chunk-type scenarios.
       Guard: chunk_type preference skipped when filter is set.
     - **Phase 3 tests:** `low_confidence` flag set/unset, graduated count formula,
       cross-tool suggestions from `content_type`, confidence floor `UnknownItem`.
     - **Phase 4 tests:** UPPERCASE symbol detection, dual-hit bonus, graduated
       staleness curve, RRF-score dedup, `asyncio.to_thread` wrapping (verify
       event loop not blocked).
     - **Latency benchmark:** cross-encoder adds <100ms on CPU for top-20 candidates.

     **Acceptance criteria:**
     - [ ] Cross-encoder enabled: measurable improvement in top-3 result relevance
       on a representative query set (manual evaluation)
     - [ ] Cross-encoder disabled: all existing tests pass, output unchanged
     - [ ] `low_confidence` flag appears in MCP responses when confidence < 0.3
     - [ ] Diversity: no more than 3 consecutive results from same repo/file
     - [ ] UPPERCASE symbols (TTS, STT, VAD) receive symbol boost
     - [ ] Event loop: index queries don't block concurrent MCP tool calls
     - [ ] docs/README.md and CLAUDE.md updated
     - [ ] `uv run pytest tests/ -v` all pass
     - [ ] `uv run ruff check src/ tests/` clean

     **Known limitations (accepted):**
     - Cross-encoder adds latency (~50-100ms per query on CPU). Optional via config.
     - Multi-concept search runs cross-encoder per-concept, not on the final
       interleaved result. A second pass on interleaved results is deferred.
     - Diversity penalty is position-based, not semantic dedup.
     - `_extract_query_symbols` still won't detect single-letter symbols.
     - Write-path `asyncio.to_thread` deferred (refresh runs blocking by design).
     - Offline: cross-encoder silently disabled if model not cached. RRF-only fallback.
  8. ~~**Language and domain filtering for example retrieval**~~ ✅ Done —
     `search_examples` returns noisy results because all `code` chunks are in
     one undifferentiated bucket. Frontend React components and Python pipeline
     bots compete for the same slots. Two ingestion-time improvements:
     - **Language metadata from file extension:** Set `language` on code chunks
       during ingestion (`.py` → `python`, `.ts`/`.tsx` → `typescript`,
       `.yaml` → `yaml`, `.json` → `json`). The `language` param already exists
       on `SearchExamplesInput` but is rarely populated on chunks. Agents can
       then filter: `search_examples(query="TTS", language="python")`.
     - **Domain tag:** Add a `domain` metadata field to code chunks:
       `backend` (Python in `src/`, `bot.py`, pipeline code),
       `frontend` (`.tsx`/`.ts`/`.jsx` in `client/`, `components/`),
       `config` (`.yaml`, `.toml`, `docker-compose.yml`),
       `infra` (Dockerfile, CI, deploy). Infer from file path + extension
       heuristics in `github_ingest.py`. Expose as a new filter param on
       `SearchExamplesInput`. Agents building Pipecat pipelines use
       `domain="backend"`, agents wiring RTVI frontends use `domain="frontend"`.
     - **Why this matters:** gradient-bang alone has ~4,600 code chunks, mostly
       frontend React/TypeScript. Without domain filtering, these dominate
       `search_examples` for any query mentioning "function calling", "timeout",
       or other terms that appear in both frontend and backend code. The
       cross-encoder helps (scores irrelevant results lower) but can't filter
       out results that weren't in the initial candidate set.
     - **Files:** `services/ingest/github_ingest.py` (set language + domain),
       `shared/types.py` (add domain filter to `SearchExamplesInput`),
       `services/retrieval/hybrid.py` (pass domain filter through),
       `services/index/vector.py` + `fts.py` (filter push-down).

## Context
Pipecat developers need grounded context for coding and ideation based on rapidly changing docs and examples. A static prompt-only approach drifts quickly and does not provide verifiable citations or reproducible outputs.

The proposed solution is a Pipecat Context Hub with:
- MCP server capabilities for tool-based retrieval and planning support.
- LLM/Codex-led orchestration that consumes MCP outputs and composes solutions.
- Retrieval-first behavior: maximize source-grounded context quality, minimize unnecessary generation.
- A continuously refreshed knowledge base for docs and example code.
- Client compatibility across local IDE/agent MCP clients.
- A single local operating mode optimized for development and hackathons.

## Requirements
1. Provide MCP tools for document and example retrieval with source citations.
2. Support local MCP transport (`stdio`) with simple local setup.
3. Maintain freshness in v0 via local `refresh` and add optional scheduled/event-driven ingestion in v1.
4. Prioritize freshness using a single `latest` index in v0.
5. Include cross-client onboarding for Claude Code, Cursor, VS Code, and Zed.
6. Optimize v0 for the primary use case: "find the right docs/examples/snippets to build a Pipecat bot."
7. Keep architecture modular so retrieval quality and storage backends can evolve independently.
8. Model foundational-example classes as first-class retrieval metadata from `pipecat/examples/foundational`.
9. Return composability guidance and dependency closure metadata in tool outputs.
10. Prioritize retrieval accuracy over synthesis; generate glue code only for explicit gaps.
11. Return explicit `known` and `unknown` items so Codex can trigger follow-up retrieval when needed.

## Implementation Checklist

### Phase 1: Foundations — T0 (serial) ✅
- [x] Project scaffolding: `pyproject.toml`, `src/` layout, dev dependencies. *(T0)*
- [x] Define canonical metadata schema as Pydantic models. *(T0)*
- [x] Define service interface protocols: `IndexWriter`, `IndexReader`, `Retriever`, `Ingester`. *(T0)*
- [x] Define tool I/O models for all v0 MCP tools. *(T0)*
- [x] Define evidence reporting models: `Citation`, `EvidenceReport`. *(T0)*
- [x] Define chunking and embedding policies for docs vs code. *(T0)*
- [x] Select vector backend with benchmark justification. *(T0)*

### Phase 2: Ingestion and Indexing — T1, T2, T3, T4 (parallel) ✅
- [x] Implement docs crawler for `docs.pipecat.ai`. *(T1)*
- [x] Implement GitHub ingest for `pipecat-ai/pipecat` and `pipecat-ai/pipecat-examples`. *(T2)*
- [x] Build fully automated taxonomy manifests. *(T3)*
  - [x] `examples/foundational` class -> example -> capability mapping (supports both subdirectory and flat file layouts).
  - [x] `pipecat-examples` capability mapping with no manual curation in v0 (root-level dir scanning).
- [x] Implement vector index + FTS index with `IndexWriter`/`IndexReader`. *(T4)*
- [x] ~~Add optional DeepWiki ingestion as a secondary source.~~ **DoA:** `llms-full.txt` provides complete official docs (305 pages) in LLM-friendly markdown format, making a third-party mirror redundant.

### Phase 3: Retrieval and Quality — T5 (parallel) ✅
- [x] Implement hybrid retrieval (vector + keyword + metadata filters). *(T5)*
- [x] Implement reranking tuned for code intent and architecture intent. *(T5)*
- [x] Add mandatory citation payload and confidence metadata. *(T5)*
- [x] Add known/unknown evidence reporting in retrieval responses. *(T5)*
- [x] Implement heuristic `next_retrieval_queries` generation. *(T5)*
- [x] Add trace logging for retrieval decisions. *(T5)*
- [x] Add capability tags and symbol maps for examples. *(T5)*
- [x] Add evidence packs that enable Claude/Codex to infer execution mode. *(T5)*

### Phase 4: MCP Server and Client Compatibility — T6, T7 (parallel) ✅
- [x] Implement MCP tools: *(T6)*
  - [x] `search_docs`
  - [x] `get_doc`
  - [x] `search_examples`
  - [x] `get_example`
  - [x] `get_code_snippet`
- [x] Implement `stdio` transport and server entry point. *(T6)*
- [x] Implement `refresh` CLI command. *(T6)*
- [x] Build client setup guides/templates for Claude Code, Cursor, VS Code, and Zed. *(T7)*

### Phase 5: Validation and Release — T8 (serial) ✅
- [x] Merge all parallel worktrees. *(T8)*
- [x] Run end-to-end integration tests. *(T8)*
- [x] Validate local retrieval-first user journeys for coding and ideation. *(T8)*
- [x] Run load and latency tests on top retrieval paths. *(T8 — 11 benchmarks, `pytest -m benchmark`)*
- [x] Publish local setup + refresh runbook. *(T8)*
- [x] Cut v0 local release. *(T8 — v0.0.1 + v0.0.2 shipped)*

### Phase 5b: Integration Seam Fixes — T10 (serial) ✅
Post-merge audit of cross-component boundaries revealed three integration seam bugs
that component-level testing missed. All fixed.

- [x] **Stale records on refresh:** `refresh` used upsert-only — deleted/renamed pages
  persisted forever. Fixed: added `clear()` to IndexStore, then refined to per-content-type
  `delete_by_content_type()` so each ingester clears only its own data before re-ingesting.
  If one ingester fails, the other's data survives.
- [x] **Unclosed HTTP client:** `DocsCrawler` opened an httpx client in `ingest()` but
  `cli.py` never called `close()`. Fixed: `await crawler.close()` in `finally` block.
- [x] **Dead `refresh()` methods:** Both ingesters and the `Ingester` protocol defined
  `refresh()` (identical to `ingest()`), but it was never called from `cli.py`. Removed
  dead code from `docs_crawler.py`, `github_ingest.py`, `interfaces.py`, and tests.

**Root cause:** Fan-out agents built correct components in isolation, but integration
seams (where one component's output feeds another's input) were never tested until T8.
`delete_by_source()` existed but wasn't wired into the refresh flow. Updated global
`/fan-out` and `/dev-plan` skills with integration seam awareness to prevent this class
of bug in future parallel agent work.

### Phase 5c: Incremental Refresh + Symbol Lookup (v0.0.7)
- [x] **Incremental refresh:** `refresh` tracks docs content hash (`docs:content_hash`)
  and per-repo commit SHAs (`repo:{slug}:commit_sha`). Unchanged sources are skipped
  entirely, reducing refresh time from ~90s to ~23s when nothing changed.
- [x] **`--force` flag:** Bypasses all skip logic for a full re-ingest.
- [x] **Per-repo deletion:** New `delete_by_repo()` on VectorIndex, FTSIndex, and
  IndexStore replaces blanket `delete_by_content_type` for changed repos only.
- [x] **Symbol lookup filter cascade:** `get_code_snippet(symbol=X)` now tries exact
  `class_name` filter, then `method_name` filter, then semantic fallback — giving
  precise matches before falling back to hybrid search.
- [x] **Error-safe caching:** Docs hash and repo SHAs are only persisted when ingest
  completes without errors, ensuring failed ingests are retried on the next run.
- [x] **Prefetched data:** CLI passes already-fetched docs text and repo paths to
  ingesters, eliminating redundant network fetches (TOCTOU fix).
- [x] **FTS error guard:** `delete_by_repo` in IndexStore catches FTS failures with
  divergence logging, matching the `delete_by_content_type` pattern.

### Phase 6: Composition Layer (v1)
- [ ] Implement `compose_solution` and `propose_architecture`.
- [ ] Add advanced guardrail inference and verification policies (minimal evidence-backed guardrails remain in v0).
- [ ] Add optional scheduled auto-refresh and expanded observability.

## Task Manifest for Parallel Execution

### Execution Model

1. **Serial: Foundation (T0)** — Orchestrator creates project scaffolding, shared types, service interface protocols, and selects vector backend. All parallel tasks depend on this completing first.
2. **Parallel: Fan-out (T1–T7)** — Independent agents in isolated git worktrees. Each task codes against shared interfaces from T0 and includes unit tests with mocks/fakes. No task modifies files owned by another task.
3. **Serial: Integration (T8)** — Orchestrator merges all worktrees, runs end-to-end integration tests, validates acceptance criteria, and cuts v0 release.

### Dependency Graph

```
T0 (foundation) ─── serial, orchestrator
 ├── T1 (ingest-docs-crawler)        ─── parallel
 ├── T2 (ingest-github-repos)        ─── parallel
 ├── T3 (ingest-taxonomy)            ─── parallel
 ├── T4 (index-store)                ─── parallel
 ├── T5 (retrieval-service)          ─── parallel
 ├── T6 (mcp-tools-and-server)       ─── parallel
 └── T7 (client-setup-guides)        ─── parallel
         │
         ▼
T8 (integration-and-release) ─── serial, orchestrator
```

All of T1–T7 depend only on T0. T8 depends on all of T1–T7.

### T0: Foundation (serial, orchestrator)

- **Description:** Create project scaffolding, shared type definitions (Pydantic models), service interface protocols, configuration schema, chunking/embedding policy definitions, and vector backend selection.
- **Owns:**
  - `pyproject.toml`
  - `src/pipecat_context_hub/__init__.py`
  - `src/pipecat_context_hub/shared/__init__.py`
  - `src/pipecat_context_hub/services/__init__.py`
  - `src/pipecat_context_hub/services/ingest/__init__.py`
  - `src/pipecat_context_hub/services/index/__init__.py`
  - `src/pipecat_context_hub/services/retrieval/__init__.py`
  - `src/pipecat_context_hub/server/__init__.py`
  - `src/pipecat_context_hub/server/tools/__init__.py`
  - `src/pipecat_context_hub/shared/types.py`
  - `src/pipecat_context_hub/shared/interfaces.py`
  - `src/pipecat_context_hub/shared/config.py`
  - `tests/__init__.py`
  - `tests/conftest.py`
  - `tests/unit/__init__.py`
  - `tests/unit/test_shared_types.py`
  - `tests/integration/__init__.py`
  - `docs/decisions/vector-backend.md`
- **Depends on:** None
- **Definition of done:**
  - `pip install -e ".[dev]"` succeeds.
  - All shared types importable and serialization round-trips pass: `ChunkedRecord`, `TaxonomyEntry`, `CapabilityTag`, `Citation`, `EvidenceReport`, `KnownItem`, `UnknownItem`, `IndexQuery`, `IndexResult`.
  - All tool I/O models importable: `SearchDocsInput`/`SearchDocsOutput`, `GetDocInput`/`GetDocOutput`, `SearchExamplesInput`/`SearchExamplesOutput`, `GetExampleInput`/`GetExampleOutput`, `GetCodeSnippetInput`/`GetCodeSnippetOutput`.
  - All service interface protocols importable: `IndexWriter`, `IndexReader`, `Retriever`, `Ingester`.
  - `pytest tests/unit/test_shared_types.py` passes.
  - Vector backend selected with benchmark notes in `docs/decisions/vector-backend.md`.

### T1: Docs Crawler (parallel)

- **Description:** Implement docs ingester that fetches `docs.pipecat.ai/llms-full.txt` (pre-rendered markdown with all 200+ pages), splits into per-page sections, cleans Mintlify XML-like tags, chunks per docs policy, and produces `ChunkedRecord` objects via `IndexWriter` interface.
- **Owns:**
  - `src/pipecat_context_hub/services/ingest/docs_crawler.py`
  - `tests/unit/test_docs_crawler.py`
- **Depends on:** T0
- **Consumes (read-only):** `shared/types.py` (`ChunkedRecord`), `shared/interfaces.py` (`IndexWriter`, `Ingester`)
- **Definition of done:**
  - `pytest tests/unit/test_docs_crawler.py` passes.
  - Ingester fetches `llms-full.txt` and produces valid `ChunkedRecord` objects for all pages.
  - Chunks respect docs chunking policy (section-aware splitting, max token limit from config).
  - All records include `source_url`, `path`, `indexed_at`, `chunk_id`, `content_type="doc"`.
  - Idempotent: re-crawling the same page produces the same `chunk_id` values.
  - Implements `Ingester` protocol.

### T2: GitHub Repo Ingester (parallel)

- **Description:** Implement ingester that clones/fetches `pipecat-ai/pipecat` and `pipecat-ai/pipecat-examples`, extracts example directories, chunks code files, and produces `ChunkedRecord` objects via `IndexWriter` interface.
- **Owns:**
  - `src/pipecat_context_hub/services/ingest/github_ingest.py`
  - `tests/unit/test_github_ingest.py`
- **Depends on:** T0
- **Consumes (read-only):** `shared/types.py` (`ChunkedRecord`), `shared/interfaces.py` (`IndexWriter`, `Ingester`)
- **Definition of done:**
  - `pytest tests/unit/test_github_ingest.py` passes.
  - Ingester processes at least one example directory from each repo.
  - Records include `repo`, `path`, `commit_sha`, `indexed_at`, `chunk_id`, `content_type="code"`.
  - Code chunking respects code policy (function/class-aware when feasible, fallback to line-based).
  - Idempotent at commit level: same commit SHA produces same records.
  - Implements `Ingester` protocol.

### T3: Taxonomy Builder (parallel)

- **Description:** Scan `pipecat/examples/foundational` and `pipecat-examples` to automatically build taxonomy manifests mapping class to example to capabilities. Fully automated, no manual curation.
- **Owns:**
  - `src/pipecat_context_hub/services/ingest/taxonomy.py`
  - `tests/unit/test_taxonomy.py`
- **Depends on:** T0
- **Consumes (read-only):** `shared/types.py` (`TaxonomyEntry`, `CapabilityTag`)
- **Definition of done:**
  - `pytest tests/unit/test_taxonomy.py` passes.
  - Produces `TaxonomyEntry` for each example in `examples/foundational` with inferred `foundational_class`.
  - Produces `TaxonomyEntry` for each example in `pipecat-examples` with capability tags.
  - Taxonomy is queryable by `foundational_class`, capability tag, or `example_id`.
  - Extraction is fully automated from directory structure, READMEs, and file content heuristics.
  - No manual curation file required.

### T4: Index Store (parallel)

- **Description:** Implement vector index and SQLite FTS5 keyword index with `IndexWriter` and `IndexReader` protocol implementations. Single `latest` namespace. Uses vector backend selected in T0.
- **Owns:**
  - `src/pipecat_context_hub/services/index/vector.py`
  - `src/pipecat_context_hub/services/index/fts.py`
  - `src/pipecat_context_hub/services/index/store.py`
  - `tests/unit/test_index_store.py`
- **Depends on:** T0
- **Consumes (read-only):** `shared/types.py` (`ChunkedRecord`, `IndexQuery`, `IndexResult`), `shared/interfaces.py` (`IndexWriter`, `IndexReader`), `docs/decisions/vector-backend.md`
- **Definition of done:**
  - `pytest tests/unit/test_index_store.py` passes.
  - `IndexWriter.upsert()` stores records and makes them searchable via both vector and keyword paths.
  - `IndexWriter.delete_by_source()` removes records by source URL.
  - `IndexReader.vector_search()` returns results ranked by embedding similarity.
  - `IndexReader.keyword_search()` returns results ranked by FTS5 relevance.
  - Metadata filters work: `repo`, `content_type`, `path` prefix, `capability_tags`.
  - Upsert is idempotent: re-indexing same `chunk_id` updates, does not duplicate.
  - Index persists to local disk (survives process restart).
  - Implements `IndexWriter` and `IndexReader` protocols.

### T5: Retrieval Service (parallel)

- **Description:** Implement hybrid retrieval combining vector and keyword search, lightweight reranking (reciprocal rank fusion + code-intent heuristics), citation assembly, and evidence reporting with known/unknown/confidence/next_queries.
- **Owns:**
  - `src/pipecat_context_hub/services/retrieval/hybrid.py`
  - `src/pipecat_context_hub/services/retrieval/rerank.py`
  - `src/pipecat_context_hub/services/retrieval/evidence.py`
  - `tests/unit/test_retrieval.py`
- **Depends on:** T0
- **Consumes (read-only):** `shared/interfaces.py` (`IndexReader`, `Retriever`), `shared/types.py` (`Citation`, `EvidenceReport`, `KnownItem`, `UnknownItem`, `RetrievalResult`)
- **Definition of done:**
  - `pytest tests/unit/test_retrieval.py` passes.
  - Hybrid search merges vector and keyword results with configurable weights.
  - Reranking applies reciprocal rank fusion + code-intent heuristics (boost exact symbol matches, penalize stale content).
  - Every result includes `Citation` with `source_url`, `repo`, `path`, `commit_sha`.
  - Every response includes `EvidenceReport` with populated `known`, `unknown`, `confidence`, `confidence_rationale`, and `next_retrieval_queries`.
  - `next_retrieval_queries` produced via deterministic heuristics (e.g., broaden failed filters, suggest related terms, widen repo scope).
  - Trace logging for retrieval decisions is emitted at DEBUG level.
  - Unit tests use mock `IndexReader` — no real index required.
  - Implements `Retriever` protocol.

### T6: MCP Tools and Server (parallel)

- **Description:** Implement all 5 v0 MCP tool handlers, `stdio` transport adapter, server entry point, and `refresh` CLI command.
- **Owns:**
  - `src/pipecat_context_hub/server/main.py`
  - `src/pipecat_context_hub/server/transport.py`
  - `src/pipecat_context_hub/server/tools/search_docs.py`
  - `src/pipecat_context_hub/server/tools/get_doc.py`
  - `src/pipecat_context_hub/server/tools/search_examples.py`
  - `src/pipecat_context_hub/server/tools/get_example.py`
  - `src/pipecat_context_hub/server/tools/get_code_snippet.py`
  - `src/pipecat_context_hub/cli.py`
  - `tests/unit/test_mcp_tools.py`
  - `tests/unit/test_server.py`
- **Depends on:** T0
- **Consumes (read-only):** `shared/interfaces.py` (`Retriever`, `Ingester`), `shared/types.py` (all tool I/O models, `EvidenceReport`)
- **Definition of done:**
  - `pytest tests/unit/test_mcp_tools.py` passes.
  - `pytest tests/unit/test_server.py` passes.
  - Each tool validates input against its contract schema and returns output matching its contract schema including `EvidenceReport`.
  - `stdio` transport sends and receives valid MCP JSON-RPC messages.
  - Server registers all 5 tools and responds correctly to `tools/list`.
  - `refresh` CLI command calls `Ingester` interface to trigger a full index rebuild.
  - `python -m pipecat_context_hub` starts the server (or equivalent entry point).
  - Unit tests use mock `Retriever` and mock `Ingester` — no real retrieval or ingestion required.

### T7: Client Setup Guides (parallel)

- **Description:** Create client configuration templates and setup instructions for Claude Code, Cursor, VS Code, and Zed.
- **Owns:**
  - `config/clients/claude-code.json`
  - `config/clients/cursor.json`
  - `config/clients/vscode.json`
  - `config/clients/zed.json`
  - `docs/setup/README.md`
  - `docs/setup/claude-code.md`
  - `docs/setup/cursor.md`
  - `docs/setup/vscode.md`
  - `docs/setup/zed.md`
- **Depends on:** T0 (needs entry point path and config schema)
- **Consumes (read-only):** `shared/config.py` (server config schema), `cli.py` (entry point)
- **Definition of done:**
  - Each config template is valid JSON and references the correct server entry point.
  - Each setup guide includes: prerequisites, install steps, config file placement, verification command.
  - Claude Code guide tested: config produces a working `stdio` connection to the server (may use mock server in isolation).
  - At least one other client guide verified for config correctness.

### T8: Integration and Release (serial, orchestrator)

- **Description:** Merge all parallel worktree branches, run end-to-end integration tests, validate all acceptance criteria, and cut v0 release.
- **Owns:**
  - `tests/integration/test_end_to_end.py`
  - `tests/integration/conftest.py`
  - Release artifacts (tag, changelog)
- **Depends on:** T1, T2, T3, T4, T5, T6, T7
- **Definition of done:**
  - All parallel task branches merge cleanly into working branch.
  - `pytest tests/` passes (all unit + integration).
  - Full pipeline works end-to-end: `refresh` → ingest → index → query → cited results.
  - `search_docs` returns relevant results for "how to create a Pipecat bot" with citations.
  - `search_examples` returns relevant examples for "wake word detection" with capability tags and foundational class metadata.
  - `get_example` returns full example content with file listing and commit citation.
  - `get_code_snippet` returns targeted code spans with dependency notes.
  - Evidence reports include meaningful `known`/`unknown` items and `next_retrieval_queries`.
  - Server starts via `stdio` and responds to MCP tool calls from a real client.
  - At least two client configs validated (Claude Code + one other).
  - All plan-level acceptance criteria met (see Acceptance Criteria section).

## Technical Specifications

### Target Architecture
1. **Ingestion service**
- Pulls docs and repository content.
- Normalizes into chunked records with deterministic IDs.
- Emits index update jobs.

2. **Knowledge store**
- Vector index + keyword index + metadata store.
- Single namespace for `latest` in v0.

3. **Retrieval service**
- Query understanding and filter planning.
- Hybrid retrieval + rerank.
- Source attribution and citation shaping.

4. **MCP core**
- Tool definitions and request orchestration.
- Transport adapters:
  - `stdio` adapter for local clients.

5. **Reasoning and orchestration layer (LLM/Codex)**
- Uses MCP evidence to infer intent, choose execution mode, and compose final design.
- Consumes structured context packs (citations, snippets, dependencies, guardrails).
- Produces user-facing plans and implementation drafts with assumptions clearly labeled.

### Source Targets (v0)
- `https://docs.pipecat.ai/` (primary docs source).
- `https://github.com/pipecat-ai/pipecat/tree/main/examples` (including `examples/foundational`).
- `https://github.com/pipecat-ai/pipecat-examples` (project-level examples).
- ~~`https://deepwiki.com/pipecat-ai/pipecat/`~~ — **Dropped.** The official `llms-full.txt` provides complete docs in LLM-friendly format; a third-party mirror adds no value.

### v0 Technology Defaults
- **Language:** Python 3.11+ (align with Pipecat ecosystem and existing examples).
- **Packaging:** `pyproject.toml` + `src/` layout.
- **Storage:** local SQLite database for metadata and FTS, with local vector index sidecar.
- **Embeddings:** local embedding model by default (no required API key in v0).
- **Reranking:** lightweight local reranking (fusion + heuristics) in v0; heavier model-based reranking deferred.
- **Runtime:** local `stdio` MCP server only in v0.
- **Ops:** local `refresh` command + logs; no webhook server or dashboard requirement in v0.

### Known / Unknown (v0 decisions)
- **Known:** Retrieval-first workflow and core tool set are in scope for March 6.
- **Known:** Fully automated taxonomy extraction from `pipecat/examples/foundational` and `pipecat-examples`.
- **Known:** `latest` is the only index in v0.
- **Unknown:** Final vector backend implementation details (to be selected during Phase 1 benchmark).
- **Resolved:** DeepWiki is not needed — `llms-full.txt` covers all 305 doc pages in LLM-friendly markdown.

### MCP Tool Contracts (v0)
1. `search_docs`
- **Purpose:** Return ranked doc chunks for conceptual/API queries.
- **Input:** `query`, optional `area`, optional `limit`.
- **Output:** Ranked hits with `doc_id`, `title`, `section`, snippet text, and citation metadata.

2. `get_doc`
- **Purpose:** Fetch a canonical doc page/section by identifier or path.
- **Input:** `doc_id` or `path` (at least one required), optional `section`.
  - `path` allows direct lookup by docs path prefix (e.g. `/guides/learn/transports`) without a prior `search_docs` call.
- **Output:** Full normalized markdown for that page/section plus source URL and indexed timestamp.

3. `search_examples`
- **Purpose:** Find relevant examples by task, modality, stack, or component.
- **Input:** `query`, optional `repo`, optional `language`, optional `tags`, optional `foundational_class`, optional `execution_mode`, optional `limit`.
- **Output:** Ranked example records with summary, foundational class, capability tags, key files, and commit metadata.

4. `get_example`
- **Purpose:** Retrieve an example package or a specific file from it for full-context understanding.
- **Input:** `example_id`, optional `path`, optional `include_readme`.
- **Output:** Example metadata and file content (full file or selected file), with repo/commit citation and detected symbols.

5. `get_code_snippet`
- **Purpose:** Return targeted code spans for reuse, not full files.
- **Input:** one of `symbol` | `intent` | `path + line range`, optional `framework`, optional `example_ids`, optional `max_lines`.
  - `intent` may be combined with `path` and/or `line_start`/`line_end` to scope the search to a specific file and line range.
- **v0 behavior:** `intent` and `path + line range` are required capabilities; `symbol` lookup is best-effort and may fall back to intent/path retrieval.
- **Output:** Minimal snippet(s) with start/end lines, dependency notes, required companion snippets, and interface expectations.

6. `search_api` *(added v0.0.3)*
- **Purpose:** Search pipecat framework source API — classes, methods, constructors, frame types.
- **Input:** `query`, optional `module` (prefix filter), optional `class_name` (prefix filter — e.g. `DailyTransport` matches `DailyTransportClient`), optional `chunk_type` (`module_overview`|`class_overview`|`method`|`function`), optional `is_dataclass`, optional `limit`.
- **Output:** Ranked `ApiHit` records with `module_path`, `class_name`, `method_name`, `base_classes`, `chunk_type`, `snippet`, `method_signature`, `is_dataclass`, citation, and score.

### MCP Tool Contracts (v1 deferred)
1. `compose_solution`
- **Purpose:** Build a capability graph by combining snippets from multiple examples and filling missing glue logic.
- **Input:** `goal`, optional `required_capabilities`, optional `constraints`, optional `target_stack`, optional `execution_mode`.
- **Output:** Composition plan with:
  - capability-to-snippet mapping
  - composability guidelines (how to connect components safely)
  - integration contracts between components
  - runtime loop design (startup, trigger handling, shutdown)
  - identified gaps requiring synthesized code only when no grounded implementation is found (for example circular frame buffer)
  - inferred guardrails and verification checks when supported by source evidence.

2. `propose_architecture`
- **Purpose:** Produce a composable implementation plan grounded in docs + examples.
- **Input:** `goal`, optional `constraints`, optional `target_stack`.
- **Output:** Architecture proposal with:
  - recommended components
  - ordered build steps
  - mapped citations
  - chosen execution mode and trigger strategy
  - snippet references (from `get_code_snippet`) and composition references (from `compose_solution`)
  - clearly labeled assumptions and generated-glue modules.

### Evidence Reporting Contract (all retrieval/composition tools)
- `known`: source-grounded facts with citation pointers.
- `unknown`: unresolved questions or missing implementation details.
- `confidence`: retrieval confidence score plus short rationale.
- `next_retrieval_queries`: deterministic heuristic suggestions from MCP in v0 (client LLM may append additional suggestions).

### Ops Plane
- **v0:** local configuration, `refresh` command, and local logs.
- **v1:** optional scheduler/webhook receiver and expanded observability.

### Intent-Mode Design
1. **One-shot**
- User asks for a bounded task (for example "explain this video clip").
- System assembles immediate pipeline and returns output once.

2. **Event-triggered**
- User asks for action when condition occurs (for example wake word, keyword, or threshold event).
- System runs lightweight monitor loop and executes task on trigger.

3. **Long-running monitor**
- User asks for continuous observation with periodic or rule-based actions.
- System defines lifecycle controls, backpressure policy, and checkpoint strategy.

### Reference Composition Flow (RTVI + Screen Share + Wake Word + Gemini Video)
This section is illustrative. The same composition and gap-handling pattern applies to other use cases and may require no synthesized glue code.

1. User intent query:
- "When wake word is spoken, analyze the last 30s of screen-share video with Gemini and speak summary via Pipecat TTS."

2. Intent inference:
- Codex classifies this as `event-triggered` with `wake-word` trigger using user intent + retrieved evidence.

3. Capability discovery:
- `search_examples` for `rtvi frontend screen share`.
- `search_examples` for `wake word` triggers.
- `search_examples` for `Gemini video` usage.
- `search_examples` for `Pipecat TTS output`.

4. Source retrieval:
- `get_example` for top matches to understand full pipeline setup.
- `get_code_snippet` for reusable fragments:
  - screen capture hooks
  - wake-word event handling
  - model invocation adapters
  - TTS response emission.

5. Composition:
- Codex composes a capability graph and integration sequence from retrieved evidence (v0).
- In v1, `compose_solution` can automate this step:
  - capture frames continuously
  - write into circular buffer sized for 30s at configured FPS
  - on wake-word event, freeze/export last window
  - call Gemini video understanding API
  - route returned text to Pipecat TTS stream.

6. Gap handling:
- If no example includes circular frame buffering, generate synthesized module spec:
  - bounded ring buffer API
  - memory/backpressure constraints
  - frame-to-video serialization strategy for Gemini input requirements.
- If grounded examples already cover the required behavior, no synthesized module is added.

7. Delivery:
- v0: Codex returns the architecture plan using retrieval evidence + known/unknown reporting.
- v1: `propose_architecture` can automate this output:
  - end-to-end architecture
  - ordered implementation plan
  - cited source mapping
  - explicit list of synthesized (non-example) components.

### Project File Layout

```
pipecat-context-hub/
├── pyproject.toml                                    # T0
├── src/
│   └── pipecat_context_hub/
│       ├── __init__.py                               # T0
│       ├── cli.py                                    # T6
│       ├── shared/
│       │   ├── __init__.py                           # T0
│       │   ├── types.py                              # T0
│       │   ├── interfaces.py                         # T0
│       │   └── config.py                             # T0
│       ├── services/
│       │   ├── __init__.py                           # T0
│       │   ├── ingest/
│       │   │   ├── __init__.py                       # T0
│       │   │   ├── ast_extractor.py                  # v0.0.3
│       │   │   ├── docs_crawler.py                   # T1
│       │   │   ├── github_ingest.py                  # T2
│       │   │   ├── source_ingest.py                  # v0.0.3
│       │   │   └── taxonomy.py                       # T3
│       │   ├── index/
│       │   │   ├── __init__.py                       # T0
│       │   │   ├── vector.py                         # T4
│       │   │   ├── fts.py                            # T4
│       │   │   └── store.py                          # T4
│       │   └── retrieval/
│       │       ├── __init__.py                       # T0
│       │       ├── hybrid.py                         # T5
│       │       ├── rerank.py                         # T5
│       │       └── evidence.py                       # T5
│       └── server/
│           ├── __init__.py                           # T0
│           ├── main.py                               # T6
│           ├── transport.py                          # T6
│           └── tools/
│               ├── __init__.py                       # T0
│               ├── search_docs.py                    # T6
│               ├── get_doc.py                        # T6
│               ├── search_examples.py                # T6
│               ├── get_example.py                    # T6
│               ├── get_code_snippet.py               # T6
│               ├── get_hub_status.py                 # v0.0.4
│               └── search_api.py                     # v0.0.3
├── config/
│   └── clients/
│       ├── claude-code.json                          # T7
│       ├── cursor.json                               # T7
│       ├── vscode.json                               # T7
│       └── zed.json                                  # T7
├── docs/
│   ├── decisions/
│   │   └── vector-backend.md                         # T0
│   └── setup/
│       ├── README.md                                 # T7
│       ├── claude-code.md                            # T7
│       ├── cursor.md                                 # T7
│       ├── vscode.md                                 # T7
│       └── zed.md                                    # T7
├── ops/                                              # v1
└── tests/
    ├── __init__.py                                   # T0
    ├── conftest.py                                   # T0
    ├── unit/
    │   ├── __init__.py                               # T0
    │   ├── test_ast_extractor.py                     # v0.0.3
    │   ├── test_hub_status.py                        # v0.0.4
    │   ├── test_shared_types.py                      # T0
    │   ├── test_docs_crawler.py                      # T1
    │   ├── test_github_ingest.py                     # T2
    │   ├── test_source_ingest.py                     # v0.0.3
    │   ├── test_taxonomy.py                          # T3
    │   ├── test_index_store.py                       # T4
    │   ├── test_retrieval.py                         # T5
    │   ├── test_mcp_tools.py                         # T6
    │   └── test_server.py                            # T6
    └── integration/
        ├── __init__.py                               # T0
        ├── conftest.py                               # T8
        └── test_end_to_end.py                        # T8
```

**Note on shared `__init__.py` files:** T0 creates all package `__init__.py` files before fan-out. Parallel tasks do not modify these files.

## Testing Notes
- Contract tests for MCP tools (input/output and error behavior).
- Ingestion tests for idempotency and incremental update correctness.
- Retrieval tests measuring relevance, citation completeness, and stale-hit rate.
- Integration tests per client type (Claude Code, Cursor, VS Code, Zed) using `stdio`.
- Performance tests for p50/p95 latency and concurrent query handling.
- Foundational taxonomy tests ensuring class filters affect example ranking and recall.
- Known/unknown contract tests verifying unresolved gaps are explicitly surfaced with follow-up retrieval suggestions.
- v1 tests: composition stitching, gap synthesis, and guardrail inference.

## Issues & Solutions
- **Issue:** Client feature parity differs by MCP client.
  - **Solution:** Keep critical workflows in MCP tools; treat prompts/resources as optional enhancements.
- **Issue:** Docs/examples drift quickly.
  - **Solution:** Use local `refresh` for v0 and add optional scheduler/webhook updates in v1.
- **Issue:** Reproducibility across local environments.
  - **Solution:** Return commit-level citations and pinned source metadata from `latest` for replayability.
- **Issue:** Over-generation can reduce trust in context-curation workflows.
  - **Solution:** Use retrieval-first responses and allow synthesis only for explicit, labeled gaps.
- **Issue:** Parallel agents creating files under shared parent directories.
  - **Solution:** T0 creates all shared `__init__.py` stubs. Each parallel task only creates files it owns. T8 resolves trivial merge conflicts.

## Acceptance Criteria
- [x] Architecture document finalized with service boundaries and data contracts.
- [x] MCP tool contract finalized and reviewed.
- [x] Freshness strategy implemented with measurable SLOs.
- [x] Local `stdio` runtime operational in at least one IDE client.
- [x] Core v0 tools operational: `search_docs`, `get_doc`, `search_examples`, `get_example`, `get_code_snippet`.
- [x] End-to-end retrieval query returns cited docs/examples with source metadata.
- [x] Local setup documented and tested across at least two MCP clients.
- [x] Foundational example class metadata is queryable and affects retrieval outcomes.
- [x] Outputs include dependency closure, composability guidance, known/unknown reporting, and guardrails when evidence supports inference.
- [x] v1 scope explicitly deferred: `compose_solution` and `propose_architecture`.

## Final Results

### v0 Implementation Complete — 2026-02-17

**Execution model:** T0 (serial) → T1–T7 (parallel fan-out in 7 git worktrees) → T8 (serial integration).

#### Test Results
- **318 tests pass**, 1 skipped (live HTTP crawl)
- **mypy strict**: 0 errors across 45 source files
- **22 integration tests** covering full ingest → embed → index → retrieve pipeline
- **Real index**: 735 records (6 docs, 729 code), 98.6% taxonomy metadata coverage

#### Components Delivered

| Phase | Task | Component | Tests |
|-------|------|-----------|-------|
| T0 | Foundation | Shared types (25+ Pydantic models), interfaces, config | 55 |
| T1 | Docs Crawler | Section-aware HTML→markdown chunking | 31 |
| T2 | GitHub Ingester | Clone/fetch repos, function/class-aware code chunking | 44 |
| T3 | Taxonomy Builder | Automated capability inference from dirs/READMEs/code/flat files | 70 |
| T4 | Index Store | ChromaDB vector + SQLite FTS5 dual-backend | 34 |
| T5 | Retrieval Service | Hybrid search, RRF reranking, evidence assembly | 43 |
| T6 | MCP Server | 5 tool handlers, stdio transport, CLI entry point | 32 |
| T7 | Client Guides | Config templates + setup docs for 4 IDE clients | — |
| T8 | Integration | Embedding service, CLI wiring, bug fixes, e2e tests | 22 |

#### Architecture
```
pipecat-context-hub refresh
  → DocsCrawler + GitHubRepoIngester + TaxonomyBuilder + SourceIngester
    → EmbeddingIndexWriter (auto sentence-transformers)
      → IndexStore (ChromaDB + SQLite FTS5)

pipecat-context-hub serve
  → IndexStore → EmbeddingService → HybridRetriever
    → MCP Server (stdio) → 7 tools
```

#### T8 Review Fixes Applied

**Code review fixes (T1–T7):**
- T1: URL dedup at enqueue time
- T2: git fetch + working tree reset, asyncio.to_thread, path traversal sanitization
- T3: manual tag source priority corrected
- T4: ChromaDB n_results clamped, FTS divergence logging
- T5: max_lines/limit conflation fixed, non-overlapping line-range guard
- T6: CLI config double-instantiation fixed
- T7: Zed config undocumented field removed
- FTS chunk_id direct lookup added for get_doc/get_example

**Taxonomy and metadata wiring (T8):**
- TaxonomyBuilder wired into GitHubRepoIngester refresh pipeline
- Chunk metadata enriched with foundational_class, capability_tags, key_files, line ranges
- execution_mode inferred from capability tags (cloud services → "cloud", else "local")
- search_examples filter contract enforced for language, foundational_class, execution_mode
- get_code_snippet non-overlapping line-range returns empty (not stale data)
- get_example returns chunk's actual path, not caller-supplied input.path

**Flat file and root-level example support (T8):**
- TaxonomyBuilder handles flat .py files in examples/foundational/ (not just subdirs)
- _find_example_dirs falls back to root-level dir scanning for repos without examples/ dir
- Per-file taxonomy lookup enables flat files to get per-file metadata enrichment

#### Remaining Items
- [x] Load/latency benchmarks on retrieval paths *(11 benchmarks added)*
- [x] ~~DeepWiki secondary source ingestion~~ — DoA: replaced by llms-full.txt
- [x] v0 release tag + changelog
- [ ] `compose_solution` and `propose_architecture` tools (v1)

### v0.0.2 — Community Repos and Ingestion Improvements (2026-02-21)

**Configurable extra repos:**
- `PIPECAT_HUB_EXTRA_REPOS` env var for adding repos without modifying source
- CLI loads `.env` from working directory on startup
- `.env.example` added with documented usage

**Ingestion for single-project repos:**
- Root fallback: repos with only filtered dirs (e.g. `src/`-layout packages
  like `pipecat-mcp-server`) now treat the repo root as a single example
- Root-level file capture: Layout B repos also index code files at the repo
  root (e.g. `sidekick.py`) alongside subdirectory examples
- Added `_iter_root_level_code_files()` helper (non-recursive scan)

**Bug fixes:**
- Root-fallback repos now get full taxonomy enrichment (`execution_mode`,
  `capability_tags`, `key_files`) — previously the `"."` key missed
- Root-level captured files inherit taxonomy from a repo-root entry
- Root-fallback scan skips `tests/`, `docs/`, `.github/`, etc. — exclusion
  uses first-component-only check so nested modules like `src/pkg/config/`
  are preserved
- `.env` parser handles inline comments and quoted values correctly
- `HubConfig` import moved to top of `cli.py` (E402)
- `get_code_snippet`: `intent` + `path` + `line_start` now accepted
  together — `path` scopes the intent search to a specific file
- Server version string corrected (0.1.0 → 0.0.2)

**Server:**
- Added MCP server instructions (uv package manager guidance)

**Test results:** 387 tests pass (35 new)

### v0.0.3 — Source API Ingester (2026-02-21)

**Motivation:** User feedback that the hub is "marginally useful at best" because
it only has high-level docs and code examples. When users need class constructors,
method signatures, frame types, or processor internals, they read `.venv` source
directly. The hub indexed `docs.pipecat.ai` (guides) and GitHub repos (examples)
but not the pipecat framework API itself.

**New: AST-based source ingester (`SourceIngester`)**
- Walks `repos/pipecat-ai_pipecat/src/pipecat/` (from existing GitHubRepoIngester
  clone) and extracts structured API metadata via Python `ast` module
- Two-tier chunking:
  - **Module overview** (1 per file): module docstring + listing of classes/functions
  - **Class overview** (1 per class): docstring + constructor + method signatures
  - **Method/function chunk** (1 per non-trivial method/function ≥3 body lines): full source + docstring
- Extracts: class names, base classes, decorators, method signatures with parameter
  types/defaults, return types, docstrings, `@dataclass`/`@abstractmethod` detection
- Chunks stored as `content_type="source"` with rich metadata: `module_path`,
  `class_name`, `chunk_type`, `base_classes`, `method_signature`, `is_dataclass`,
  `is_abstract`, `line_start`/`line_end`
- Runs as step 3 in refresh, after GitHubRepoIngester guarantees fresh clone

**New: `search_api` MCP tool**
- Hybrid retrieval over `content_type="source"` chunks
- Filters: `module` (prefix match), `class_name`, `chunk_type`, `is_dataclass`
- Returns `ApiHit` objects with `module_path`, `base_classes`, `method_signature`, `snippet`

**New types:** `SearchApiInput`, `ApiHit`, `SearchApiOutput`

**Index backend updates:**
- ChromaDB: Added `module_path`, `class_name`, `chunk_type`, `is_dataclass`,
  `is_abstract`, `base_classes` to metadata serialization; native `$eq` filters
  for `chunk_type`, `is_dataclass`; `module_path` and `class_name` prefix post-filters
- SQLite FTS5: Added LIKE clauses for `class_name` (prefix), `chunk_type` (exact),
  `module_path` (prefix), `method_name` (exact); boolean `is_dataclass` filter on metadata_json

**Execution model:** T0 (serial: types + interfaces) → T1–T4 (parallel fan-out
in 4 git worktrees) → T8 (serial integration + review fixes)

| Task | Component | Files |
|------|-----------|-------|
| T0 | Foundation types | `types.py`, `interfaces.py` |
| T1 | AST extractor | `ast_extractor.py`, `test_ast_extractor.py` |
| T2 | Source ingester | `source_ingest.py`, `test_source_ingest.py` |
| T3 | Index backends | `vector.py`, `fts.py` |
| T4 | Retrieval + tool + CLI | `hybrid.py`, `search_api.py`, `main.py`, `cli.py` |
| T8 | Integration | Conflict resolution, review fixes, `test_server.py` |

**Review fixes applied:**
- `build_signature()` returned `def name(params)` but callers prepended name
  again → doubled names in module/class overview chunks. Fixed: returns
  `(params) -> ReturnType` only, callers prepend `def name` where needed
- `_get_commit_sha()` missing `timeout` on subprocess.run → could block
  indefinitely. Fixed: `timeout=10`
- `mock_retriever` fixture missing `search_api` return value → search_api
  dispatch untested. Fixed: added `SearchApiOutput` mock with `ApiHit`
- mypy type narrowing for `kw_defaults[i]` in AST extractor
- FTS `module_path` filter: exact-match → prefix-match
- `_make_chunk_id()`: added `line_start` to disambiguate duplicate
  class/method names in same module (pipecat source has overloaded methods
  and re-opened classes)
- `base_classes` metadata: comma-join → JSON string (lossless for generics)
- `rel_path`: `str()` → `as_posix()` for cross-platform Windows support
- `chunk_type` field description: added missing `'function'` type

**Refresh results:** 454 files → 5,075 source chunks, 10,017 total index
(3,520 docs + 1,422 code + 5,075 source)

**New files:**
- `src/pipecat_context_hub/services/ingest/ast_extractor.py` (334 lines)
- `src/pipecat_context_hub/services/ingest/source_ingest.py` (380 lines)
- `src/pipecat_context_hub/server/tools/search_api.py` (18 lines)
- `tests/unit/test_ast_extractor.py` (495 lines)
- `tests/unit/test_source_ingest.py` (519 lines)

**Test results:** 475 tests pass (88 new)

### v0.0.4 — Improve Tool Invocation and Add Index Freshness (2026-02-26)

**Motivation:** User feedback from testing the MCP server with Claude Code:
1. Claude doesn't proactively invoke MCP tools — defaults to reading `.venv`
   source directly instead of using `search_api`, `search_examples`, etc.
2. No temporal context — tool responses don't indicate when the index was last
   refreshed, what pipecat version is indexed, or how many records exist.

**Part A: Improved server instructions and tool descriptions**
- `_SERVER_INSTRUCTIONS` expanded with tool routing guide: tells Claude which
  tool to use for each query pattern and explicitly says "always use these
  tools instead of reading .venv"
- All 6 existing tool descriptions rewritten to be action-oriented with
  use-case hints (e.g. `search_docs`: "Use for 'how do I...?' questions")

**Part B: Persistent index metadata and `get_hub_status` tool (7th tool)**
- New `index_metadata` SQLite table for persisting key-value metadata across
  server restarts
- `FTSIndex` gains `set_metadata()`, `get_metadata()`, `get_all_metadata()`,
  `get_index_stats()` methods; proxied through `IndexStore`
- CLI `refresh` persists: `last_refresh_at`, `last_refresh_duration_seconds`,
  `records_upserted`, `error_count`, `content_type_counts`
- New `get_hub_status` MCP tool returns: server version, last refresh
  timestamp, duration, record counts by type, commit SHAs, index path
- `create_server()` accepts optional `index_store` for status tool dispatch

**Review fixes (3 rounds: Codex + self-review):**
- Conditional tool registration: `get_hub_status` only listed when
  `index_store` is provided (split `_BASE_TOOLS` + `_HUB_STATUS_TOOL`)
- Success-gated metadata: `last_refresh_at` only on success; failed
  refreshes write `last_refresh_errored_at`
- `IndexStore.data_dir` public property replaces private `_fts._sqlite_path`
  access in handler
- Single `_SERVER_VERSION` constant shared by server and handler (no
  duplicate)
- Handler typed as `IndexStore` instead of `Any`

| File | Action |
|------|--------|
| `server/main.py` | Edit (instructions, descriptions, `_BASE_TOOLS`/`_HUB_STATUS_TOOL` split, `_SERVER_VERSION`, conditional registration) |
| `server/tools/get_hub_status.py` | Create (imports `_SERVER_VERSION`, typed `IndexStore`, uses `data_dir`) |
| `services/index/fts.py` | Edit (metadata table + methods + stats) |
| `services/index/store.py` | Edit (metadata/stats proxy methods + `data_dir` property) |
| `shared/types.py` | Edit (GetHubStatusInput, HubStatusOutput) |
| `cli.py` | Edit (persist metadata success-gated, pass index_store) |
| `tests/unit/test_hub_status.py` | Create (15 tests) |
| `tests/unit/test_server.py` | Edit (tool count/names, conditional registration, restored assertion) |

**Part D: RRF score normalization (0–1)**
- Raw RRF scores (~0.03 for docs) caused evidence module to always report
  "low relevance" — thresholds (`HIGH=0.5`, `LOW=0.1`) were calibrated for
  cosine similarity, not RRF scale
- `reciprocal_rank_fusion()` now divides by theoretical max
  (`num_lists / (k + 1)`): rank 1 in both lists → 1.0 (was 0.033)
- Final scores clamped to [0, 1] after symbol boost / staleness penalty
- Evidence module thresholds now trigger correctly without code changes

**Part E: Pipecat import persistence**
- `ast_extractor.py` already extracts `module_info.imports` but
  `source_ingest.py` never stored them — one-line fix adds filtered
  pipecat imports to module_overview metadata
- Imports flattened as JSON string in ChromaDB (same pattern as
  `base_classes`)
- New `imports` field on `ApiHit` surfaces imports in `search_api` results

| File | Action |
|------|--------|
| `services/retrieval/rerank.py` | Edit (normalize RRF to 0–1, clamp after heuristics) |
| `services/ingest/source_ingest.py` | Edit (persist pipecat imports in module_overview) |
| `services/index/vector.py` | Edit (flatten imports for ChromaDB) |
| `shared/types.py` | Edit (add `imports` field to `ApiHit`) |
| `services/retrieval/hybrid.py` | Edit (populate imports in search_api hits) |
| `tests/unit/test_retrieval.py` | Edit (update RRF score expectations) |

**Test results:** 507 tests pass, lint clean

## v0.0.5 — Multi-Concept Query Decomposition

**Branch:** `feature/multi-concept-search` | **PR:** #7

**Problem:** Compound queries like "idle timeout + function calling + Gemini"
return poor results — the single embedding matches no chunk well, and all
top-N results cluster around whichever concept dominates.

**Solution:** When explicit delimiters (` + ` or ` & `) are detected, split
the query into sub-concepts, run per-concept searches in parallel, and
interleave results for balanced coverage. Single-concept queries are unchanged.

### Design Decisions

- **Delimiters:** Only ` + ` and ` & ` — comma and "and" were removed after
  review because they produce false positives ("error handling, logging",
  "search and replace")
- **Parallel execution:** Per-concept searches via `asyncio.gather`
- **Interleaving:** Round-robin across concepts with deduplication by chunk_id
- **Limit allocation:** Ceiling division; falls back to single-concept when
  `limit < n` to avoid over-fetching
- **Test mocks:** Dispatch by `query.query_text` (not call order) for
  deterministic `asyncio.gather` behavior

### Files

| File | Action |
|------|--------|
| `services/retrieval/decompose.py` | Create (pure function `decompose_query()`) |
| `services/retrieval/hybrid.py` | Edit (refactor `_hybrid_search` → dispatcher + single/multi) |
| `tests/unit/test_retrieval.py` | Edit (16 new tests) |

**Test results:** 522 tests pass, lint clean

## v0.0.6 — Multi-Repo Source Ingestion + `get_code_snippet` Retrieval Fix ✅

**Released:** 2026-03-03 | **PRs:** #8, #9, #10

### Part 1: Multi-Repo Source Ingestion ✅

**Problem:** `SourceIngester` only indexes `pipecat-ai/pipecat` — the repo
slug, clone directory, and `src/` path are all hardcoded. Other repos with
`src/` layouts (e.g. `pipecat-ai/pipecat-agents`, `pipecat-ai/gradient-bang`,
`vr000m/pipecat-mcp-server`) are cloned by `GitHubRepoIngester` but their
source code never gets AST-indexed. `search_api("BaseAgent")` returns nothing
despite `pipecat-agents` being cloned.

**Solution:** Parameterize `SourceIngester` to accept a `repo_slug`, auto-
discover `src/` packages in each cloned repo, and loop over all
`effective_repos` in the CLI.

#### Design Decisions

- **Constructor takes `repo_slug`:** Replaces hardcoded `_REPO_SLUG` constant
- **Auto-discovery:** Finds package dirs under `src/` (dirs with `__init__.py`)
- **Silent skip:** Repos without `src/` return 0 records with no error — normal
  for example-only repos
- **Per-repo logging:** Only logs repos that produce records; silent otherwise
- **Source label:** `IngestResult.source` now includes repo slug
  (`"source:pipecat-ai/pipecat"`) for clearer diagnostics

#### Files

| File | Action |
|------|--------|
| `services/ingest/source_ingest.py` | Edit (parameterize repo slug, auto-discover src/) |
| `cli.py` | Edit (loop over `effective_repos`) |
| `tests/unit/test_source_ingest.py` | Edit (update constructor calls, +2 new tests) |

**Test results:** 527 tests pass, lint clean

### Part 2: `get_code_snippet` Symbol Lookup Fix ✅

**Problem:** `get_code_snippet(symbol="MLXModel")` returns GLSL noise utility
code instead of the Python enum. Root causes:

1. **Wrong content_type filter:** `get_code_snippet` always filtered by
   `content_type="code"` (example code), but framework classes like `MLXModel`
   are in `content_type="source"` records from `SourceIngester`.
2. **`symbol` filter silently dropped:** The retriever added
   `filters["symbol"]` but neither vector nor FTS backends handle that key —
   search degraded to pure embedding similarity with no symbol constraint.

**Solution:** Route by lookup mode — symbol lookups now search
`content_type="source"` (framework API definitions), intent lookups keep
`content_type="code"` (example code). Removed the broken `symbol` filter;
the symbol name as query text provides sufficient embedding + keyword signal.

#### Design Decisions

- **Symbol → source, intent → code:** Maps to the semantic purpose of each
  lookup mode. Users searching for a class definition want framework source;
  users describing what they want to do want example code.
- **Removed broken filters:** `framework` and `example_ids` filters were also
  silently dropped by both index backends — removed from symbol path to avoid
  confusion. These remain as documented-but-unimplemented features.
- **Path filter in symbol mode:** Now accepted as an optional narrowing filter
  (e.g., `symbol="MLXModel", path="pipecat/services/whisper/"`)

#### Files

| File | Action |
|------|--------|
| `services/retrieval/hybrid.py` | Edit (route content_type by lookup mode) |
| `tests/unit/test_retrieval.py` | Edit (update symbol test, +4 new tests) |

**Test results:** 530 tests pass, lint clean

### Part 3: ChromaDB Batch Operations Fix ✅

**Problem:** Multi-repo ingestion pushed source record count to 6,185, exceeding
ChromaDB's ~5,461 per-call limit with `BatchSizeExceededError`.

**Solution:** All vector index operations (`upsert`, `delete_by_content_type`,
`delete_by_source`) now batch in chunks of `_CHROMA_BATCH_SIZE = 5000`.

### Part 4: Review Fixes ✅

Four findings from Codex review, all fixed:
- **P1:** Slug sanitization mismatch — `_sanitize_slug()` now uses same `re.sub` regex as `GitHubRepoIngester`
- **P1:** `path+line_start` snippet mode restored `content_type="code"` scope
- **P2:** `_make_chunk_id` now includes `repo_slug` — prevents cross-repo overwrites
- **P2:** Import metadata filter removed hardcoded `"pipecat"` substring

### Part 5: Version Consistency + Stress Tests ✅

- Added `TestVersionConsistency` — asserts `pyproject.toml` version matches `_SERVER_VERSION` using `tomllib`
- Added `TestVectorIndexBatchStress` — 4 stress tests with 5,100 records above batch limit
- Documented versioning convention in `CLAUDE.md`

**Final test results:** 541 tests (526 core + 15 benchmarks), lint clean

## Retrieval UX Improvements (2026-03-26) ✅

Based on feedback from Claude instances using the context hub in real coding
sessions. Two recurring friction points addressed:

### Part 1: Path-Based `get_doc` Lookup ✅

Users called `search_docs` just to get a `doc_id`, then called `get_doc` with it.
When they already knew the path (e.g. `/guides/learn/transports`), the extra
round-trip was unnecessary.

- Added `path` field to `GetDocInput` as alternative to `doc_id`
- Model validator requires at least one of `doc_id` or `path`
- `HybridRetriever.get_doc` routes to FTS path-prefix lookup when `path` is
  provided and `doc_id` is empty
- Tool description updated to document both lookup modes

### Part 2: `class_name` Prefix Matching ✅

`search_api("send_dtmf", class_name="DailyTransport")` returned nothing because
the method lives on `DailyTransportClient`. Users don't always know the exact
subclass name.

- FTS: changed `class_name` from exact JSON LIKE pattern (`%"class_name": "X"%`)
  to prefix pattern (`%"class_name": "X%`) — omits closing quote
- Vector: moved `class_name` from ChromaDB `$eq` push-down to post-filter with
  `.startswith()`, added to `needs_post_filter` set for 3× over-fetch
- Both backends now match consistently: `DailyTransport` finds
  `DailyTransport`, `DailyTransportClient`, `DailyTransportParams`
- Field descriptions on `SearchApiInput` and `GetCodeSnippetInput` updated

### Fast Follow (not in this PR)

- **Daily SDK dict schemas** — `.pyi` uses `Mapping[str, Any]` which hides
  parameter details (e.g. `send_dtmf` settings dict fields like `tones`,
  `duration`, `digitDurationMs`). Would need Daily RST doc parsing or
  manual annotation layer.

**Test results:** 680 passed, 0 failed, lint clean

## RST Type Documentation Indexing (in progress)

### Problem

The `daily-co/daily-python` `.pyi` stub uses `Mapping[str, Any]` for dict
parameters throughout: `send_dtmf(settings: Mapping[str, Any])`,
`join(settings: Mapping[str, Any])`, `start_dialout(settings: Mapping[str, Any])`,
etc. Agents see the method exists but cannot see what keys the dict accepts
(`tones`, `sessionId`, `digitDurationMs`, `method`).

The actual dict schemas are documented in `docs/src/types.rst` within the
`daily-co/daily-python` repo — 72 type definitions, 1307 lines. This is
structured RST using `.. list-table::` directives with Key/Value columns,
inline union literals, and cross-references between types.

### Data Available

Three RST type patterns in `types.rst`:

1. **Dict types** (most common) — `.. list-table::` with Key/Value header:
   ```rst
   .. _DialoutSendDtmfSettings:

   DialoutSendDtmfSettings
   -----------------------------------

   .. list-table::
      :widths: 25 75
      :header-rows: 1

      * - Key
        - Value
      * - "sessionId"
        - string
      * - "tones"
        - string
      * - "method"
        - "sip-info" | "telephone-event" | "auto"
      * - "digitDurationMs"
        - number
   ```

2. **Enum/union types** — inline literal:
   ```rst
   CallState
   -----------------------------------

   "initialized" | "joining" | "joined" | "leaving" | "left"
   ```

3. **Simple aliases** — prose:
   ```rst
   CallClientError
   -----------------------------------

   A string with an error message or *None*.
   ```

4. **"Or" alternates** — two `.. list-table::` blocks under the same heading
   separated by a bare `or` line (exactly 2 instances: `AudioInputSettings`,
   `VideoInputSettings`). Parser must merge as alternative shapes.

Also: `api_reference.rst` uses `.. autoclass::` directives (Sphinx autodoc)
which reference the same `.pyi` classes. Less useful since we already index
the stubs directly.

### Approach

Add RST type parsing as a new module alongside `SourceIngester`. When a repo
has `.rst` files in `docs/` containing `.. list-table::` type definitions,
parse them into structured chunks.

**Why SourceIngester, not DocsCrawler?** These are API type definitions (like
class/struct definitions), not conceptual documentation. They should appear in
`search_api` results alongside the method signatures that reference them.

**Cross-referencing:** Explicit method-to-type linkage is implemented via a
static mapping table (`daily_type_map.py`) that populates `related_types`
metadata on `.pyi` method chunks at ingestion time. Surfaced as
`related_type_defs` on `get_code_snippet` results and `related_types` on
`search_api` results. The static table sidesteps the fragile
method-name-to-type-name heuristic problem (e.g. `join()` →
`MeetingTokenProperties`, not `JoinSettings`).

### Chunk Design

Each RST type definition becomes a `content_type="source"` chunk with:

- `chunk_type="type_definition"` (new chunk type)
- `class_name` = type name (e.g. `DialoutSendDtmfSettings`)
- `module_path` = derived from repo (e.g. `daily`)
- `path` = `docs/src/types.rst`
- `content` = human-readable rendering of the type:
  ```
  # Type: DialoutSendDtmfSettings
  Module: daily

  Dict type with fields:
  - "sessionId": string
  - "tones": string
  - "method": "sip-info" | "telephone-event" | "auto"
  - "digitDurationMs": number
  ```
- `metadata.fields` = JSON list of `{key, value_type}` for structured access
- `metadata.rst_refs` = cross-referenced type names (e.g. `DialoutCodecs`)

RST inline markup (backtick cross-refs, external hyperlinks, parenthetical
descriptions) is stripped during parsing. Compound value types like
`[ "PCMU" | "OPUS" ]` and `bool | number` are stored as raw type strings
after markup removal — no attempt to parse compound types into structured form.

Type names, field keys, and refs are normalized and length-limited before
indexing per the AGENTS.md security constraint (non-AST ingestion source).

### What This Enables (v1)

- `search_api("send_dtmf settings")` → returns both the method signature AND
  `DialoutSendDtmfSettings` type definition (via embedding similarity)
- `search_api("DialoutSendDtmfSettings")` → direct lookup of the dict schema
- `search_api("DialoutSendDtmfSettings", chunk_type="type_definition")` →
  filtered to type definitions only
- `get_code_snippet(symbol="DialoutSendDtmfSettings")` → full type definition

### Implementation Checklist

- [x] Create `src/pipecat_context_hub/services/ingest/rst_type_parser.py` —
      extract type definitions from `.rst` files (keeps `source_ingest.py`
      focused on orchestration)
- [x] Handle all four RST type patterns: dict/list-table, enum/union, alias,
      and "or" alternates (merge as alternative shapes)
- [x] Strip RST inline markup: backtick cross-refs, external hyperlinks,
      parenthetical descriptions, Sphinx roles (`:class:`, `:func:`),
      and control characters
- [x] Normalize and length-limit type names, field keys, refs, and
      descriptions before indexing (security: non-AST ingestion source)
- [x] Wire into `SourceIngester.ingest()` — scan `docs/` for `.rst` files,
      call parser, build `content_type="source"` chunks
- [x] Set `path = "docs/src/types.rst"` and verify `_make_source_url` produces
      valid GitHub URLs with line ranges for RST files
- [x] Add `chunk_type="type_definition"` to `SearchApiInput.chunk_type` Literal
- [x] Add `fields` and `rst_refs` metadata serialization to
      `_record_to_metadata` and `_metadata_to_record_fields` in `vector.py`
- [x] Update `search_api` tool description in `server/main.py` to mention
      `type_definition` as a valid `chunk_type` filter
- [x] Update `type_definition` in reranking chunk-type preference in `rerank.py`
- [x] Unit tests for RST parsing (all 4 patterns + edge cases)
- [ ] Test `.rst` discovery and combined `.pyi` + `.rst` ingestion in
      `test_source_ingest.py` — verify the `source_ingest.py` early-return
      gate does not block `.rst` discovery
- [ ] Update `test_mcp_tools.py` with `type_definition` filter test
- [ ] Live MCP smoke test — mixed-query retrieval regression:
      `search_api("send_dtmf settings")` returns both the `.pyi` method
      signature AND `DialoutSendDtmfSettings` type definition
- [ ] Live MCP smoke test — direct lookup:
      `search_api("DialoutSendDtmfSettings", chunk_type="type_definition")`
      returns the dict schema
- [ ] Run full AGENTS.md pre-merge smoke suite (all 12 items)

### Scope Constraints

- Only parse `.. list-table::` and inline union/alias patterns — no general
  RST rendering engine needed
- Only runs on repos that have `.rst` files in `docs/` — no impact on other repos
- `api_reference.rst` (Sphinx autodoc) is skipped — we already have the `.pyi`
- ~~**No cross-referencing in v1**~~ ✅ Done. Static `daily_type_map.py`
  populates `related_types` metadata on `.pyi` method chunks. Surfaced via
  `related_type_defs` (CodeSnippet) and `related_types` (ApiHit) at
  retrieval time in `hybrid.py`.

### Future Enhancements (v2)

These were identified during review but deferred to keep v1 focused on the
core value (making dict schemas discoverable):

- ~~**Explicit cross-referencing**~~ ✅ Done. Implemented via
  `daily_type_map.py` (46 static mappings) + `related_types` metadata +
  `related_type_defs` / `related_types` output fields.
- ~~**companion_snippets linkage**~~ ✅ Done. `related_types` is surfaced as
  a dedicated `related_type_defs` field on `CodeSnippet` (separate from
  `companion_snippets` which remains derived from `calls`).
- **Two-stage search_api retrieval** — when `class_name` filter is set,
  expand results to include related type definitions that wouldn't pass the
  class_name prefix filter. E.g. `search_api("join", class_name="CallClient")`
  returns both `CallClient.join` AND `ClientSettings` / `MeetingTokenProperties`.

### Files to Modify

- `src/pipecat_context_hub/services/ingest/rst_type_parser.py` — new file, RST parsing
- `src/pipecat_context_hub/services/ingest/source_ingest.py` — wire RST parser
- `src/pipecat_context_hub/shared/types.py` — `chunk_type` Literal update
- `src/pipecat_context_hub/services/index/vector.py` — `fields`/`rst_refs` metadata serialization
- `src/pipecat_context_hub/services/retrieval/rerank.py` — chunk-type preference for `type_definition`
- `src/pipecat_context_hub/server/main.py` — tool description update
- `tests/unit/test_rst_type_parser.py` — new file, RST parsing tests
- `tests/unit/test_source_ingest.py` — `.rst` discovery + combined ingestion test
- `tests/unit/test_mcp_tools.py` — `type_definition` filter test

## Multi-Language API Extraction (planned)

### Problem

The hub only extracts API metadata (classes, methods, signatures) from Python
via `ast_extractor.py`. TypeScript, Swift, Kotlin, and C++ repos are indexed
as raw code chunks but have no structured API surface in `search_api`. This
means:

- **TypeScript** client SDKs (`pipecat-client-web`, `pipecat-client-web-transports`,
  `voice-ui-kit`) — 900+ chunks as code but zero API extraction
- **Swift** iOS SDKs (7 repos) — 0 chunks total
- **Kotlin** Android SDKs (3 repos) — 2-3 chunks (READMEs only)
- **C++** native SDKs (`pipecat-client-cxx`, `pipecat-esp32`) — 2-3 chunks

### Architecture

Three-layer ingestion stack per language, each adding value independently:

```
┌─────────────────────────────────────────┐
│ Layer 3: Doc comments (///, /** */)     │  ← language-specific regex
│ Layer 2: Tree-sitter AST               │  ← full API extraction
│ Layer 1: Regex source parsing           │  ← lightweight extraction
│ Layer 0: Code chunks (existing)         │  ← GitHubRepoIngester
└─────────────────────────────────────────┘
```

**Current state per language:**

| Language | Layer 0 | Layer 1 | Layer 2 | Layer 3 |
|----------|---------|---------|---------|---------|
| Python | ✅ | ✅ `ast_extractor.py` | N/A (uses `ast`) | ✅ docstrings via AST |
| TypeScript | ✅ (859+ chunks) | ❌ | ❌ | ❌ JSDoc not extracted |
| Swift | ❌ 0 chunks | ❌ | ❌ | ❌ |
| Kotlin | ❌ 2-3 chunks | ❌ | ❌ | ❌ |
| C++ | ❌ 2-3 chunks | ❌ | ❌ | ❌ |

### Review Findings (2026-03-29)

Two independent reviews (Claude + Codex) identified these issues with the
original plan. All findings have been incorporated into the revised plan below.

**Critical — `.d.ts` files do not exist in checked-out repos:**
The original Phase 1a assumed `.d.ts` declaration files would be available.
Investigation of the actual cloned repos found:
- `pipecat-client-web`: 0 `.d.ts` files (only `.ts` source in `client-js/`)
- `pipecat-client-web-transports`: 1 file (`vite-env.d.ts` — boilerplate)
- `voice-ui-kit`: 2 files (`vite-env.d.ts` — boilerplate)
`.d.ts` files are build artifacts generated to `dist/`, which is in
`_SKIP_DIRS` and not committed to source repos. Phase 1a is re-scoped to
parse TypeScript source (`.ts`/`.tsx`) directly using regex extraction.

**Important — metadata schema needs non-Python mappings:**
`SearchApiInput.chunk_type` is a strict Literal enum (`module_overview`,
`class_overview`, `method`, `function`, `type_definition`). Fields like
`is_dataclass`, `yields`, and `calls` are Python-specific. Plan now includes
explicit TS→chunk_type mapping and documents which fields remain empty.

**Important — `SourceIngester` hardcoded for Python:**
`source_ingest.py` only discovers root `.pyi`, `src/` Python packages, and
`docs/*.rst`. TS repos use different layouts (`client-js/`, `client-react/`,
`package/src/`, `transports/*/src/`). Plan now includes a concrete TS
discovery contract.

**Important — `metadata["language"]` hardcoded to `"python"`:**
Every metadata dict in `_build_chunks()` has `"language": "python"`. New
parsers must set this correctly.

### Phased Approach

#### Phase 1: Quick wins without tree-sitter

Lightweight parsers using regex and file-type detection. No new dependencies.

**1a. TypeScript source parsing (regex-based)**

Parse exported declarations from `.ts`/`.tsx` source files using regex.
NOT `.d.ts` declaration files (see review findings above — these are build
artifacts not present in source repos).

Target constructs:
- `export interface Foo { ... }` / `export default interface`
- `export class Bar extends Baz { ... }` / `export abstract class`
- `export type Qux = ...`
- `export function doSomething(...): ReturnType`
- `export type Callback = (...) => ReturnType`
- `export const thing: Type = ...` (only typed exports)

Repos and their source layouts (verified from local clones):
- `pipecat-client-web`: monorepo with `client-js/` (core SDK: `client/`,
  `rtvi/`) and `client-react/` (React hooks: `src/`)
- `pipecat-client-web-transports`: `transports/*/src/` (Daily, WebSocket,
  WebRTC, Gemini) + `lib/` (shared utilities)
- `voice-ui-kit`: `package/src/` (components, visualizers, stores)

TS source discovery contract for `source_ingest.py`:
1. Detect TS repos: presence of `package.json` or `tsconfig.json` at root
2. Find TS roots: scan for directories containing `.ts`/`.tsx` files,
   excluding `node_modules/`, `dist/`, `build/`, `tests/`, `examples/`
3. Module path derivation: repo-relative path with `/` separator,
   e.g., `client-js/client/client` for `client-js/client/client.ts`
4. Only parse files with `export` statements (skip internal modules)

Chunk type mapping (TS → existing `chunk_type` values):
- TS `interface` → `class_overview` (closest semantic match)
- TS `class` → `class_overview`
- TS `type` alias → `type_definition`
- TS exported `function` → `function`
- TS `enum` → `type_definition`

Method extraction is deferred to Phase 2 (tree-sitter). Regex-based method
extraction from class bodies is brittle — TS classes use complex signatures
with generics, overloads, and decorators that regex cannot reliably parse.
Phase 1a produces `class_overview` chunks that include the full class body
as snippet (including method signatures), which is sufficient for
`search_api` discoverability. Phase 2 tree-sitter will emit individual
`method` chunks with proper `method_name` and `method_signature` metadata.

Python-specific fields that remain empty for TS chunks:
- `is_dataclass`: always `False`
- `yields`: always `[]`
- `calls`: always `[]` (could be populated in Phase 2 with tree-sitter)
- `decorators`: always `[]` (TS decorators are rare in these repos)

All TS chunks MUST use `content_type="source"` (required by `search_api`
filter in `hybrid.py` line 612) and `metadata["language"] = "typescript"`.

File: new `ts_source_parser.py` alongside `rst_type_parser.py`

**1b. Doc comment extraction (all languages)**

Language-agnostic regex extraction of `///`, `/** */`, and `#` doc comments
above class/function declarations. Store as enrichment metadata on existing
code chunks, not separate chunks.

- `///` — Swift, C++, Rust
- `/** ... */` — TypeScript (JSDoc), Kotlin (KDoc), Java
- `"""..."""` — Python (already extracted by AST)

Note: For languages with zero code chunks (Swift, Kotlin), doc comment
extraction is a no-op until tree-sitter phases produce chunks to attach to.
Phase 1b is primarily useful for TypeScript repos that already have 900+
code chunks.

**1c. README indexing for zero-chunk repos**

Repos with 0 code chunks (Swift, some Kotlin) should at minimum have their
README indexed so `search_docs` can find them.

- Create standalone `ChunkedRecord` objects with `content_type="doc"`
  (NOT `"readme"` — `search_docs` in `hybrid.py` hard-filters to
  `content_type="doc"`, so `"readme"` chunks would be invisible)
- Check if README is already indexed by `GitHubRepoIngester` — may need
  to add `.md` to `_CODE_EXTENSIONS` or handle separately

#### Phase 2: Tree-sitter for TypeScript

Add `tree-sitter` and `tree-sitter-typescript` as dependencies. Build a
language-agnostic extraction framework that replaces the Phase 1a regex parser.

- **New dependency**: `tree-sitter` + `tree-sitter-typescript`
- **New module**: `src/pipecat_context_hub/services/ingest/ts_extractor.py`
  or a generic `tree_sitter_extractor.py`
- **Extracts**: classes, interfaces, type aliases, exported functions,
  method signatures with parameter types, return types, generics
- **Biggest impact**: `pipecat-client-web` (core RTVI SDK), `voice-ui-kit`
  (React components), `pipecat-flows-editor` (visual editor)

#### Phase 3: Tree-sitter for Swift

Add `tree-sitter-swift`. Extract protocols, structs, classes, enums,
functions, and `public` access control.

- 7 iOS SDK repos go from 0 → hundreds of API chunks
- Protocol conformance maps to `base_classes` metadata
- `@objc` / `@available` decorators inform API stability

#### Phase 4: Migrate Python from `ast` to tree-sitter

Replace `ast_extractor.py` with tree-sitter-based extraction. Benefits:
- Unified parser infrastructure for all languages
- Better error recovery on malformed files
- Consistent metadata format across languages

Risk: `ast_extractor.py` is well-tested (verify actual count with
`pytest --collect-only tests/unit/test_ast_extractor.py`). Migration must
be backward-compatible — same chunks, same metadata, same test results.

#### Phase 5: Tree-sitter for Kotlin and C++

Add `tree-sitter-kotlin` and `tree-sitter-cpp`. Smallest user base,
lowest priority.

### Implementation Checklist

Phase 1:
- [ ] TS source parser (`ts_source_parser.py`) — regex-based extraction of
      exported interfaces, classes, types, functions from `.ts`/`.tsx` files
- [ ] Wire TS parser into `source_ingest.py` — TS repo detection, source
      root discovery, module path derivation
- [ ] Doc comment extraction (regex-based, language-agnostic)
- [ ] README indexing for zero-chunk repos (standalone `content_type="doc"`)
- [ ] Unit tests for TS parser (interfaces, classes, types, functions, generics)
- [ ] MCP smoke tests: `search_api("PipecatClient")`,
      `search_api("WebSocketTransport")`, `search_api("Transport")` must
      return TS symbols with correct repo constraints (not Python hits)

Phase 2:
- [ ] Add `tree-sitter` + `tree-sitter-typescript` dependencies
- [ ] Build TypeScript AST extractor (replaces Phase 1a regex parser)
- [ ] Index `pipecat-client-web`, `voice-ui-kit`, `pipecat-flows-editor`
- [ ] Live MCP smoke tests for TypeScript API search

Phase 3:
- [ ] Add `tree-sitter-swift`
- [ ] Build Swift AST extractor
- [ ] Index iOS SDK repos
- [ ] Live MCP smoke tests for Swift API search

Phase 4:
- [ ] Migrate Python `ast_extractor.py` to tree-sitter
- [ ] Backward-compatibility validation (same chunks, same metadata)
- [ ] Performance comparison

Phase 5:
- [ ] Add `tree-sitter-kotlin` + `tree-sitter-cpp`
- [ ] Build Kotlin and C++ AST extractors
- [ ] Index Android and native SDK repos

### Scope Constraints

- Phase 1 has zero new dependencies — regex/file-type detection only
- Tree-sitter phases add one dependency per language grammar
- Each phase is independently shippable and releasable
- Python AST migration (Phase 4) is optional — only if tree-sitter proves
  better on the TS/Swift phases first
- `search_api` filters (`module`, `class_name`, `chunk_type`) work unchanged
  for all languages — TS constructs map to existing chunk_type values (see
  mapping above). Python-specific fields (`is_dataclass`, `yields`, `calls`)
  remain empty for non-Python chunks.
- All non-Python API chunks MUST use `content_type="source"` and set
  `metadata["language"]` correctly (not hardcoded to `"python"`)

### Files to Modify (Phase 1)

- `src/pipecat_context_hub/services/ingest/ts_source_parser.py` — new,
  regex-based extraction of TS exported declarations
- `src/pipecat_context_hub/services/ingest/doc_comment_extractor.py` — new
- `src/pipecat_context_hub/services/ingest/source_ingest.py` — add TS repo
  detection, TS source root discovery, wire `ts_source_parser`, parameterize
  `metadata["language"]`
- `src/pipecat_context_hub/services/ingest/github_ingest.py` — README
  handling for zero-chunk repos, add missing extensions to
  `_EXTENSION_TO_LANGUAGE` (`.swift`, `.kt`, `.cpp`, `.h`, `.hpp`)
- `tests/unit/test_ts_source_parser.py` — new, parser unit tests
- `tests/unit/test_doc_comment_extractor.py` — new
- `tests/unit/test_source_ingest.py` — add TS repo discovery tests
