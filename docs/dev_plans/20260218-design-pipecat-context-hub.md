# Pipecat Context Hub Architecture Plan

## Header
- **Status:** Complete (v0)
- **Type:** design
- **Assignee:** vr000m
- **Priority:** High
- **Working Branch:** feature/pipecat-context-hub
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
- More advanced reranking and guardrail inference.
- Optional scheduled auto-refresh and richer local observability.
- Decide and document refresh failure policy: **empty-on-failure** (current v0 behavior — stale data is worse than missing data for LLM context) vs **retain-previous-on-failure** (keep last-known-good records when ingestion fails). May require snapshot/swap semantics in IndexStore.
- Version-pinned ingestion: allow pinning to a specific pipecat release tag instead of always ingesting HEAD. Track index-level metadata (pipecat version, docs fetch timestamp) so users building against older pipecat versions get matching context. Warn when the indexed pipecat version diverges from the user's installed version.

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
- **Purpose:** Fetch a canonical doc page/section by identifier.
- **Input:** `doc_id`, optional `section`.
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
- **Input:** `query`, optional `module` (prefix filter), optional `class_name`, optional `chunk_type` (`module_overview`|`class_overview`|`method`|`function`), optional `is_dataclass`, optional `limit`.
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
  for `class_name`, `chunk_type`, `is_dataclass`; `module_path` prefix post-filter
- SQLite FTS5: Added LIKE clauses for `class_name`, `chunk_type`, `module_path`,
  `method_name`; boolean `is_dataclass` filter on metadata_json

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

**Test results:** 507 tests pass, lint clean
