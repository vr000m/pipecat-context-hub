# Changelog

All notable changes to the Pipecat Context Hub are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/).

## [0.0.8] - Unreleased

### Added

- **Call-graph metadata** on method/function chunks: `yields` (frame types
  yielded) and `calls` (methods called via `self.method()`,
  `ClassName.method()`, `super().method()`) extracted from AST and stored
  as structured list fields
- **`yields` and `calls` filters** on `search_api` — agents can query
  "methods that yield TTSAudioRawFrame" or "methods that call push_frame"
  directly instead of falling back to `.venv` source reads
- **`yields` and `calls` fields** on `ApiHit` output — structured lists
  surfaced through MCP tool responses
- **Pipecat-internal import propagation** to class overview and method chunks,
  including relative imports (`from .utils import X`) — module overview retains
  full imports list
- **`## Yields` / `## Calls` sections** appended to method chunk text content
  for FTS keyword searchability
- `_walk_body_shallow()` iterative DFS walker that restricts extraction to
  executable function bodies — excludes decorators, parameter defaults, return
  annotations, nested functions, lambdas, and nested classes

### Changed

- `_extract_yields` only processes `ast.Yield` (not `ast.YieldFrom`) — generator
  delegation names are not frame types and were breaking the `yields` contract
- FTS `yields`/`calls` filters use JSON-key-anchored LIKE patterns with quoted
  values and closing `]` to prevent cross-field false positives
- Vector `yields`/`calls` filters use list membership post-filter (not substring
  matching on JSON dumps) for exact-match semantics
- `_extract_imports` preserves relative import dots (`from .utils` no longer
  stripped to `from utils`) via `node.level`
- `needs_post_filter` in `VectorIndex.search()` updated to include `yields`
  and `calls` for over-fetch when post-filtering

## [0.0.7] - 2026-03-11

### Added

- **Incremental refresh**: `refresh` now tracks docs content hash and per-repo
  commit SHAs. Unchanged sources are skipped entirely, reducing refresh time
  from ~90s to ~23s when nothing changed
- **`--force` flag** on `refresh` command to bypass all skip logic and force a
  full re-ingest
- **`delete_by_repo()`** on `VectorIndex`, `FTSIndex`, and `IndexStore` for
  targeted per-repo index cleanup (replaces blanket `delete_by_content_type`
  for changed repos)
- **Symbol lookup filter cascade** in `get_code_snippet`: tries exact
  `class_name` filter, then `method_name` filter, then semantic fallback —
  gives precise class/method matches before falling back to hybrid search
- `method_name` filter support in `VectorIndex._build_where_clause`
- `delete_metadata()` on `FTSIndex` and `IndexStore` for removing stale
  metadata keys (e.g. cached SHAs for removed repos)
- **Removed-repo cleanup**: `refresh` detects repos no longer in
  `effective_repos` and deletes their stale index data and metadata
- **`module` and `class_name` filters** on `get_code_snippet`: symbol lookups
  can be scoped by module path prefix (e.g. `module='pipecat.runner.daily'`)
  and/or class name, matching the filtering already available in `search_api`
- **`content_type` override** on `get_code_snippet`: intent and path lookups
  can set `content_type='source'` to search framework code instead of examples
- **`max_length` constraints** on all MCP tool string input fields to prevent
  oversized inputs reaching SQLite LIKE and ChromaDB queries
- **`chunk_type` Literal enum** on `SearchApiInput` — rejects invalid values
  at validation time and exposes the enum in the JSON schema
- **Per-element tag constraint** on `SearchExamplesInput.tags` — each tag
  capped at 64 characters

### Changed

- `get_code_snippet` `max_lines` default raised from 50 to 100 — covers 97%
  of method chunks without truncation (P90=56, P95=77 across 4,268 indexed
  methods).  Large methods like `configure()` (180 lines) still need an
  explicit `max_lines=200+`
- `search_docs` `area` filter now maps to a path prefix query (previously
  accepted but silently ignored by both index backends)
- `get_example` `include_readme` now returns stored `readme_content` from
  chunk metadata (previously always None due to ingest gap — content is now
  stored during GitHub ingestion, capped at 64 KB)
- Tool descriptions for `search_docs`, `get_doc`, `search_examples`,
  `search_api`, and `get_code_snippet` updated to document available filters
  and parameter usage

- `refresh` now ingests repos individually for per-repo error tracking instead
  of batch-ingesting all changed repos at once
- `clone_or_fetch` and `fetch_llms_txt` made public APIs (called by CLI for
  incremental hash/SHA comparison before deciding to ingest)
- CLI passes prefetched data (docs text, repo paths) to ingesters, eliminating
  redundant network fetches during refresh

### Fixed

- Docs content hash no longer persisted after errored ingest — prevents
  skipping broken docs on the next run
- Repo commit SHA no longer persisted after errored ingest — prevents skipping
  failed repos on the next run
- All `IndexStore` delete methods (`delete_by_content_type`, `delete_by_repo`,
  `delete_by_source`) now wrap FTS calls in error guards with divergence logging
- Cached repo SHA invalidated when `--force` ingest fails — prevents the next
  non-force refresh from skipping a repo left empty by a transient failure
- LIKE metacharacters (`%`, `_`, `\`) now escaped in all FTS filter patterns —
  prevents silent filter bypass from user input containing wildcards
- Explicit `device="cpu"` on `SentenceTransformer` init — avoids torch 2.10+
  meta tensor errors in long-running MCP server processes

### Removed

- Dead `path` field from `GetExampleInput` (was declared but never read)
- Dead `framework` and `example_ids` fields from `GetCodeSnippetInput`

## [0.0.6] - 2026-03-06

### Added

- **Multi-repo source indexing**: `SourceIngester` parameterized by repo slug —
  all repos with `src/` layouts now get AST-indexed, not just `pipecat-ai/pipecat`
- **Flat example file indexing**: repos with `.py` files directly in `examples/`
  (no subdirectories) are now discovered and indexed

### Changed

- `get_code_snippet` symbol lookups now search `content_type="source"` (framework
  API definitions) instead of `content_type="code"` (examples) — fixes symbol
  queries like `symbol="MLXModel"` returning irrelevant example code
- ChromaDB upsert, delete_by_content_type, and delete_by_source operations batched
  in chunks of 5,000 to avoid `BatchSizeExceededError` with large record counts
- Multi-concept query guidance added to tool descriptions and CLAUDE.md
- `_SERVER_VERSION` constant used in hub status test assertions (no more hardcoded
  version strings)

### Fixed

- Slug sanitization in source ingester matches `GitHubRepoIngester` — prevents
  silent skips for slugs with dots or special characters
- `content_type="code"` filter restored on path+line_start snippet mode —
  prevents returning source records when paths overlap
- Repo slug included in source chunk IDs — prevents cross-repo overwrites when
  repos share module names (forks)
- Import filter no longer hardcoded to "pipecat" — non-pipecat repos retain
  full API context
- Single-letter concepts (e.g. "C + concurrency") now decompose correctly
  (`MIN_CONCEPT_LENGTH` lowered from 2 to 1)

## [0.0.5] - 2026-02-28

### Added

- **Multi-concept query decomposition**: compound queries like
  "idle timeout + function calling + Gemini" now decompose into sub-concepts,
  run per-concept searches in parallel, and interleave results for balanced
  coverage. Use ` + ` or ` & ` as delimiters
- **RRF score normalization**: scores now divided by theoretical maximum so
  top results score ~1.0 instead of ~0.03 — evidence thresholds trigger
  correctly and confidence reports are meaningful
- **`imports` field on `ApiHit`**: `search_api` results include pipecat-internal
  imports for each module, enabling "what uses this class?" discovery
- `IndexStore.data_dir` property for clean index path access

### Changed

- `get_hub_status` only registered when `index_store` is provided — prevents
  broken MCP contract for old call sites
- `last_refresh_at` only written on fully successful refreshes (0 errors) —
  failed refreshes record `last_refresh_errored_at` instead
- Final reranked scores clamped to [0, 1] after heuristic adjustments
- Server instructions expanded with multi-concept query guidance
- License changed from MIT to BSD-2-Clause

### Fixed

- Multi-concept decomposition restricted to ` + ` and ` & ` delimiters only —
  comma and "and" caused false positives on natural language queries
- Ampersand delimiter requires surrounding spaces (`\s+&\s+`) — prevents
  splitting names like "AT&T"
- Ceiling division for per-concept candidate allocation — fixes under-allocation
  when limit isn't evenly divisible by concept count
- Round-trip imports in vector metadata reconstruction — `search_api` results
  from vector path no longer return empty imports
- `import json` moved to module level in `hybrid.py` — fixes potential
  `NameError` in conditional branch

## [0.0.4] - 2026-02-26

### Added

- **`get_hub_status` MCP tool** (7th tool): returns index health metadata —
  server version, last refresh timestamp, refresh duration, record counts by
  content type, distinct commit SHAs, and index data path
- **Persistent index metadata**: new `index_metadata` SQLite table stores
  key-value pairs (last refresh time, duration, record counts, error count)
  that survive across server restarts
- `FTSIndex.set_metadata()`, `get_metadata()`, `get_all_metadata()`,
  `get_index_stats()` methods for metadata CRUD and index statistics
- `IndexStore` proxies all metadata/stats methods to FTS backend
- New shared types: `GetHubStatusInput`, `HubStatusOutput`
- **`imports` field on `ApiHit`**: `search_api` results now include
  pipecat-internal imports for each module, enabling "what uses this class?"
  discovery
- **Pipecat imports persisted** in source `module_overview` chunks — filtered
  to `pipecat.*` imports only, stored in both FTS and ChromaDB backends

### Changed

- **Server instructions** expanded with tool routing guide — tells Claude
  which tool to use for each query pattern (conceptual → `search_docs`,
  examples → `search_examples`, API internals → `search_api`, etc.) and
  explicitly instructs "always use these tools instead of reading .venv"
- **Tool descriptions** rewritten to be action-oriented with use-case hints
  (e.g. `search_docs` now says "Use for 'how do I...?' questions")
- `create_server()` accepts optional `index_store` parameter for
  `get_hub_status` dispatch; tool is only listed when store is provided
- CLI `refresh` command now persists metadata after each successful run
  (failed refreshes record `last_refresh_errored_at` instead)
- CLI `serve` command passes `index_store` to `create_server`
- Single `_SERVER_VERSION` constant shared by server and handler
- `IndexStore.data_dir` property exposes index path without private access
- **RRF scores normalized to 0–1** — `reciprocal_rank_fusion()` now divides
  by theoretical maximum (`num_lists / (k + 1)`).  Top-ranked results that
  appear in both vector and keyword lists score 1.0 instead of ~0.03.
  Downstream evidence reports now correctly classify results as "strong" or
  "moderate" relevance instead of always reporting "low relevance"
- Final reranked scores clamped to [0, 1] after symbol boost / staleness
  penalty adjustments

## [0.0.3] - 2026-02-21

### Added

- **Source API ingester**: AST-based extraction of structured API metadata from
  the pipecat framework source (`src/pipecat/`). Produces three chunk types —
  module overview, class overview, and method/function — stored as
  `content_type="source"`. Extracts class names, base classes, decorators,
  method signatures with parameter types/defaults, return types, docstrings,
  and `@dataclass`/`@abstractmethod` detection (454 files, 5,075 chunks)
- New MCP tool `search_api` for searching framework internals (constructors,
  method signatures, frame types, processor APIs) with filters for `module`
  (prefix), `class_name`, `chunk_type` (`module_overview`, `class_overview`,
  `method`, `function`), and `is_dataclass`
- New shared types: `SearchApiInput`, `ApiHit`, `SearchApiOutput`
- `Retriever` protocol extended with `search_api` method
- ChromaDB and SQLite FTS5 index backends support new metadata fields:
  `module_path`, `class_name`, `chunk_type`, `base_classes`, `method_signature`,
  `is_dataclass`, `is_abstract`

### Fixed

- `build_signature()` no longer prepends `def name` — callers control the
  prefix, preventing doubled names in module/class overview chunks
- `_get_commit_sha()` now has `timeout=10` to prevent indefinite blocking
- `_make_chunk_id()` includes `line_start` to disambiguate duplicate
  class/method names within the same module (e.g. overloaded methods,
  re-opened classes in pipecat source)
- FTS `module_path` filter changed from exact-match to prefix-match, aligning
  with the vector backend and `search_api` contract
- mypy type narrowing for `kw_defaults[i]` in AST extractor (local variable
  assignment before None check)
- `base_classes` metadata stored as JSON string instead of comma-join,
  preventing corruption for generics like `Base[Foo, Bar]`
- `rel_path` in source ingester uses `as_posix()` for cross-platform
  compatibility (Windows backslashes no longer break module path derivation)
- `chunk_type` field description updated to include `'function'`

## [0.0.2] - 2026-02-21

### Added

- `PIPECAT_HUB_EXTRA_REPOS` environment variable for adding community repos
  without modifying source code (comma-separated slugs, appended to defaults
  with deduplication)
- CLI loads `.env` from the working directory on startup (explicit env vars
  take precedence)
- `.env.example` with documented usage
- Single-project repo ingestion: repos with no qualifying subdirectories
  (e.g. `src/`-layout packages) now fall back to treating the repo root as
  a single example — all code files are indexed recursively
- Root-level code file capture for Layout B repos: entry-point scripts
  (e.g. `sidekick.py`) sitting at the repo root are now indexed alongside
  subdirectory examples
- MCP server instructions (uv package manager guidance for LLM clients)

### Fixed

- `get_code_snippet` now accepts `intent` combined with `path` and
  `line_start` — `path` acts as an optional filter scoping the intent search
  to a specific file, and `line_start`/`line_end` trim results to the
  requested range
- Root-fallback repos (`src/`-layout) now get full taxonomy enrichment
  (`execution_mode`, `capability_tags`, `key_files`) — previously the
  taxonomy lookup keyed by `"."` missed, producing unenriched chunks that
  broke filtered retrieval (e.g. `execution_mode="local"` returned 0 hits)
- Root-level captured files (e.g. `sidekick.py` in Layout B repos) now
  inherit taxonomy metadata from a repo-root entry — previously the per-file
  lookup always missed, leaving chunks without `execution_mode`/`capability_tags`
- Root-fallback ingestion now skips `tests/`, `docs/`, `.github/`, and other
  non-source directories — previously `_iter_code_files` only skipped build
  artifacts, polluting example search with test and CI files.  The exclusion
  is applied only to the **first** path component relative to the scan root,
  so nested modules with the same name (e.g. `src/pkg/config/settings.py`)
  are preserved
- `.env` parser now correctly handles inline comments and quoted values —
  `KEY="val" # note` previously included `" # note` in the value, producing
  malformed repo slugs
- `HubConfig` import moved to top of `cli.py` (fixes E402 lint violation)
- Server version string corrected from `0.1.0` to match package version

## [0.0.1] - 2026-02-19

Initial release — local-first MCP server providing Pipecat docs and examples
context for Claude Code, Cursor, VS Code, and Zed.

### Added

- MCP server with stdio transport and 5 retrieval tools: `search_docs`,
  `get_doc`, `search_examples`, `get_example`, `get_code_snippet`
- Docs ingestion from `docs.pipecat.ai/llms-full.txt` (305 pages, 3,996 chunks)
- GitHub repo ingestion for `pipecat-ai/pipecat` and `pipecat-ai/pipecat-examples`
  (729 code chunks)
- TaxonomyBuilder with automatic capability tag inference from directory names,
  README content, and Python imports/class references
- Hybrid retrieval: ChromaDB vector search + SQLite FTS5 keyword search with
  Reciprocal Rank Fusion reranking
- Local embeddings via `all-MiniLM-L6-v2` (sentence-transformers, no API key)
- `refresh` CLI command for full index rebuild with delete-before-ingest
  (stale records never persist across refreshes)
- Client config templates for Claude Code, Cursor, VS Code, and Zed
- Runtime warning when a discovered example dir has no taxonomy entry

### Fixed

- Mixed-layout repos (e.g. `examples/foundational/` + `examples/quickstart/`)
  get full taxonomy coverage — `TaxonomyBuilder.build_from_directory()` no longer
  short-circuits to foundational-only

### Known limitations

- Refresh always ingests from HEAD of configured repos — no version pinning
  (planned for v1)
- If ingestion fails after delete, that content type stays empty until next
  successful refresh (empty-on-failure policy; retain-previous-on-failure
  deferred to v1)
