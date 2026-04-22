# Changelog

All notable changes to the Pipecat Context Hub are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses [Semantic Versioning](https://semver.org/).

## [0.0.18] - 2026-04-21

### Security

- **lxml GHSA-vfmq-68hx-4jfw / CVE-2026-41066** — bumped `lxml` to `>=6.1.0`
  to close an XXE vector in the default configuration of `iterparse()` and
  `ETCompatXMLParser()` (`resolve_entities=True` allowed local-file reads).
  `lxml` enters the lockfile transitively via `cyclonedx-bom`; the 4.x line
  pinned `lxml<6`, so the dev floor was raised to `cyclonedx-bom>=7.3,<8.0`
  (pulls `cyclonedx-python-lib` 11.x, which allows `lxml<7`) and an explicit
  `lxml>=6.1.0` dev pin was added so future transitive bumps cannot regress
  below the patched version.

### Changed

- **`serve` lifetime knobs are now first-class `ServerConfig` fields** —
  `idle_timeout_secs` and `parent_watch_interval_secs` join the existing
  `transport` and `log_level` fields on `ServerConfig`, with env-aware
  computed properties matching the rest of `HubConfig`. Env-var
  resolution moved out of `transport.py` into `shared/config.py` for
  consistency. `parent_watch_interval_secs` is now floored at `0.1s`
  when non-zero (prevents misconfigured tiny values from CPU-spinning
  on `os.getppid()`). No user-visible behaviour change.

### Added

- **Idle-timeout shutdown for `serve`** — the server now exits on its
  own when no MCP tool dispatch arrives for `PIPECAT_HUB_IDLE_TIMEOUT_SECS`
  seconds (default `1800`, i.e. 30 minutes). Catches the production
  failure mode the parent-death watchdog cannot: when the client stays
  alive but stops using a hub it spawned without closing the pipe (the
  case responsible for most accumulated zombies under `uv run`). Set
  `PIPECAT_HUB_IDLE_TIMEOUT_SECS=0` to disable. Logs
  `idle_timeout idle_seconds=N timeout_seconds=N` at INFO when it fires.

### Fixed

- **Orphan `serve` processes no longer accumulate** (direct-invocation
  path) — a parent-death watchdog inside the stdio transport polls
  `os.getppid()` every 2s and triggers a clean shutdown when the MCP
  client disappears without closing stdio. The PPID is snapshotted at
  CLI entry (before IndexStore / embedding / reranker construction) so
  client deaths during startup are still detected. On trigger, stdin is
  forcibly closed to unblock MCP's internal `stdin_reader` task,
  allowing the `stdio_server` context manager to unwind and the
  `IndexStore` finally-block to close handles cleanly. The watchdog
  logs `parent_died original_ppid=N current_ppid=1` at INFO when it
  fires. Honors hidden env var `PIPECAT_HUB_PARENT_WATCH_INTERVAL`
  (seconds, default `2.0`) for tests. Disabled on Windows where
  orphan-reparent semantics differ — stdin EOF still works there.

  **Known gap:** when `serve` is launched via `uv run
  pipecat-context-hub serve` (the default in this project's docs and
  in most MCP-client configs), `uv` stays alive as an intermediate
  parent and the inner Python process's PPID never flips — the
  parent-death watchdog does not fire. The new idle-timeout (above)
  covers this case as a backstop. For instant cleanup on parent
  death, configure your MCP client to launch Python directly
  (e.g. `.venv/bin/pipecat-context-hub serve`); see the README's
  "MCP client configuration" section for examples.

## [0.0.17] - 2026-04-20

### Added

- **Configurable cross-encoder reranker model** — new
  `PIPECAT_HUB_RERANKER_MODEL` env var selects between three allowlisted
  cross-encoder models without editing Python config:
  `cross-encoder/ms-marco-MiniLM-L-6-v2` (~80 MB, default, balanced),
  `cross-encoder/ms-marco-MiniLM-L-12-v2` (~130 MB, higher quality), and
  `cross-encoder/ms-marco-TinyBERT-L-2-v2` (~17 MB, fastest download, lower
  quality). Unknown values log a warning and fall back to the default — the
  server never fails to start on a misconfigured env var. Useful on slow or
  throttled HuggingFace Hub connections where the default model download
  would stall.
- `get_hub_status` now surfaces live reranker runtime state (not just
  configured intent): `reranker_enabled` reflects whether the reranker
  is actually active, `reranker_model` is the active model name,
  `reranker_configured_model` is what the operator requested, and
  `reranker_disabled_reason` (`config_disabled` | `not_cached` |
  `load_failed`) explains degraded runs. Lets operators diagnose cases
  where the selected model is not cached or failed to load without
  reading server logs.
- **Reranker disabled-at-startup warning** — when `serve` boots with the
  reranker off, a single consolidated `WARNING` log line now reports
  `reason=<config_disabled|not_cached> configured_model=<name>` plus a
  one-line remediation hint. For `not_cached`, the hint names the exact
  HuggingFace cache directory that was probed (resolved through
  `HF_HOME` / `HUGGINGFACE_HUB_CACHE` when set), so operators can spot
  cache-discovery mismatches without reading library internals.
  Operators can grep this from an MCP JSONL trace to diagnose degraded
  boots without calling `get_hub_status`.
- **Startup banner** — `serve` now logs one `INFO` line at boot reporting
  version, data directory, and the raw `counts_by_type` mapping
  (`doc` / `code` / `source` keys as they appear in FTS, rendered as
  `counts_by_type={doc=N,code=N,source=N}`). Confirms which binary is
  actually running after an upgrade and exposes partially-populated
  indexes without a separate tool call. The `data_dir` path is redacted
  to `~/…` because server instructions now encourage clients to share
  startup log lines with maintainers.
- **Degraded-hub reporting guideline** — server instructions now direct
  MCP clients to share the full `get_hub_status` response and startup
  log lines with the user (and point them at the bug-report template)
  when the hub is running in a degraded mode — specifically
  `reranker_disabled_reason ∈ {not_cached, load_failed}` or a non-zero
  boot exit. `config_disabled` is explicitly called out as a supported
  operator choice, not a degraded state, so intentional
  `PIPECAT_HUB_RERANKER_ENABLED=0` deployments are not escalated as
  incidents.

### Changed

- **`serve` fails fast on unusable indexes** — the server now exits with a
  non-zero status and a clear remediation hint when the index is empty
  (zero records) or cannot be opened, instead of starting up and silently
  returning no results. MCP clients previously hung waiting for meaningful
  responses; they now see stdio close at boot and can surface a real
  error. Run `pipecat-context-hub refresh` before `serve` on a fresh
  install; use `refresh --force --reset-index` to rebuild a corrupt index.

### Fixed

- **Corrupt clone recovery** — `refresh` now detects repo clones left in a
  half-initialized state (e.g., `.git/objects/pack/` present but `HEAD` /
  `config` / `refs/` missing after an interrupted clone), re-clones them,
  and force-re-ingests the repo even when its remote SHA matches the
  previously stored one (the prior `stored_sha == commit_sha` skip path
  would otherwise keep the empty/broken corpus in place). Affected users
  previously saw zero code/source chunks from the broken repo with no
  obvious signal. The refresh summary reports recovered repos.
- **Non-UTF-8 console safety** — `refresh`'s summary table no longer crashes
  with `UnicodeEncodeError` on Windows consoles whose code page cannot
  encode the non-ASCII glyphs it uses (U+2500 box-drawing rule, U+2014 em
  dash in empty SHA/count cells). Every such glyph is probed against the
  active `sys.stdout.encoding` and falls back to ASCII `-` when it cannot
  be encoded; UTF-8 terminals and OEM code pages that support the glyph
  (e.g. cp437 supports U+2500) are unchanged. Set `PYTHONIOENCODING=utf-8`
  to opt into the full Unicode output on Windows.

## [0.0.16] - 2026-04-07

### Added

- **Version-pinned framework indexing (Phase 4)** — `refresh --framework-version v0.0.96`
  (or `PIPECAT_HUB_FRAMEWORK_VERSION=v0.0.96` env var) indexes the framework
  repo at a specific git tag instead of HEAD. Users pinned to an older pipecat
  version get `search_api` results matching their installed API surface.
  Deprecation map stays forward-looking from HEAD/release-notes.
- `get_hub_status` now surfaces the pinned `framework_version` in its output
- **Release notes deprecation parsing** — `check_deprecation` now uses
  GitHub release notes as a primary source for deprecation data. Parses
  `### Deprecated` and `### Removed` sections from pipecat releases,
  extracts module paths and class names from backtick-wrapped text, and
  populates the deprecation map with version-attributed entries. This
  replaces the now-empty `DeprecatedModuleProxy` source as the primary
  data for `check_deprecation`.
- Release notes are fetched via `gh` CLI during `refresh` (graceful
  fallback when `gh` is unavailable)
- Replacement path extraction from "use X instead" patterns

## [0.0.15] - 2026-04-05

### Added

- **Version-aware retrieval (Phase 2)** — `search_examples`, `search_api`,
  and `get_code_snippet` now accept an optional `pipecat_version` parameter
  (e.g., `"0.0.95"`). When provided, results are scored for compatibility
  and annotated with `version_compatibility`:
  - `compatible` — user's version satisfies the chunk's constraint
  - `newer_required` — chunk requires a version the user hasn't upgraded to
  - `older_targeted` — chunk targets an older version the user has passed
  - `unknown` — no version constraint on the chunk
- **Version filtering** — `version_filter="compatible_only"` on
  `search_examples` and `search_api` excludes `newer_required` results
  (with over-fetch to maintain result count)
- **Combined penalty cap** — version penalty (`-0.05`) and staleness penalty
  are capped at `-0.10` combined, preventing double-penalization of old +
  incompatible results. Highly relevant incompatible examples still rank
  above irrelevant compatible ones.

## [0.0.14] - 2026-04-04

### Added

- **Version-aware indexing (Phase 1a)** — extract `pipecat-ai` version
  constraints from `pyproject.toml`, `requirements.txt`, and `package.json`
  during ingestion. Per-example-directory walk-upward supports monorepos
  (e.g., pipecat-examples). Framework repo derives version from
  `git describe --tags`. Stored as `pipecat_version_pin` on chunk metadata
  and surfaced in `ExampleHit`, `ApiHit`, `CodeSnippet` results.
- **`check_deprecation` MCP tool (Phase 1b)** — new tool to check whether
  a pipecat import path is deprecated. Parses `DeprecatedModuleProxy` usage
  (with bracket-expansion) from framework source and CHANGELOG
  `Deprecated`/`Removed` sections. Fuzzy symbol matching (prefix, exact,
  child). Built at `refresh`, persisted as JSON, loaded at `serve` startup.
- `packaging` added as an explicit dependency (was transitive only)

### Changed

- Server instructions now recommend `check_deprecation` when pipecat
  imports are seen
- Deprecation map automatically deleted when framework repo is not in
  `effective_repos` (prevents serving stale data)

### Security

- Symlink rejection and `resolve().relative_to()` containment guards on
  all new file-read paths: deprecation source scanner, version extraction
  manifest readers, and CHANGELOG reader
- Narrowed `except Exception` to `except (InvalidRequirement, TypeError)`
  in version parsers (bandit B112)

## [0.0.13] - 2026-03-31

### Changed

- **Tree-sitter TypeScript extraction (Phase 2)** — replaced the regex
  parser with tree-sitter-based AST extraction. Individual method chunks
  with full typed signatures now indexed for classes and interfaces.
  Supports `.ts` and `.tsx` files with separate grammar selection.
- **Method-level search** — `search_api("connect", class_name="PipecatClient")`
  now returns the individual method chunk with signature and body
- **Enhanced metadata** — `method_signature`, `return_type`, `imports`,
  and `calls` populated for TS function and method chunks
- **Removed regex parser** — `ts_source_parser.py` deleted, fully replaced
  by `ts_tree_sitter_parser.py`

### Added

- `tree-sitter` and `tree-sitter-typescript` runtime dependencies

## [0.0.12] - 2026-03-30

### Added

- **TypeScript source parsing (Phase 1a)** — regex-based extraction of
  exported interfaces, classes, type aliases, functions, enums, and typed
  const exports from `.ts`/`.tsx` files with JSDoc comment inclusion
- **6 core TS SDK repos added to default ingestion** —
  `pipecat-client-web`, `pipecat-client-web-transports`, `voice-ui-kit`,
  `pipecat-flows-editor`, `web-client-ui`, `small-webrtc-prebuilt`
- **`language="typescript"` metadata** on all TS source chunks for
  language-aware filtering in `search_api`
- **README fallback for zero-chunk repos (Phase 1c)** — repos with no code
  files (e.g. iOS/Android SDKs) now have their README indexed as
  `content_type="doc"` so they're discoverable via `search_docs`
- **Swift, Kotlin, C++ extension mappings** in `_EXTENSION_TO_LANGUAGE`
  for correct language metadata on code chunks

## [0.0.11] - 2026-03-29

### Added

- **Method-to-type cross-referencing** — Daily SDK `.pyi` method chunks now
  include `related_types` metadata linking methods to their RST type
  definitions (e.g. `send_dtmf` → `DialoutSendDtmfSettings`). Surfaced via
  `related_type_defs` on `get_code_snippet` and `related_types` on
  `search_api` results. 46 method-to-type mappings for CallClient and
  EventHandler.
- **MCP audit and hardening workflow** — committed CI quality/security jobs,
  a written MCP threat model, `just audit`, `just sbom`, and an opt-in
  runtime stability benchmark for repeated `refresh` / `serve` cycles and
  concurrent retrieval rounds
- **Local upstream taint controls** — `PIPECAT_HUB_TAINTED_REPOS` and
  `PIPECAT_HUB_TAINTED_REFS` let operators skip compromised repos, tags, or
  commit SHAs locally without waiting for upstream removal
- **Path-based `get_doc` lookup** — `get_doc(path="/guides/learn/transports")`
  returns the full assembled page without requiring a prior `search_docs` call.
  Multi-chunk pages are concatenated in insertion order with section extraction
  working on the assembled content.
- **`class_name` prefix matching** — `search_api` and `get_code_snippet` now
  match `class_name` as a prefix: `DailyTransport` finds `DailyTransport`,
  `DailyTransportClient`, `DailyTransportParams`. Both FTS and Vector backends
  updated consistently.
- **RST type documentation indexing** — `search_api` now indexes type
  definitions from `.rst` files (e.g. `types.rst` in `daily-co/daily-python`).
  Filter with `chunk_type="type_definition"` to find dict schemas, enums, and
  aliases alongside method signatures. Parses 72 Daily SDK type definitions
  including `DialoutSendDtmfSettings`, `ClientSettings`, `RecordingStreamingSettings`.
- **Pre-merge live MCP smoke test** — 10-item checklist in AGENTS.md for
  verifying retrieval correctness against the live local index before merging
- **Security policy** — `SECURITY.md` added with vulnerability reporting
  instructions and supported-version table
- **Curated `.env.example` repo bundles** — copy-ready
  `PIPECAT_HUB_EXTRA_REPOS` examples are now grouped by SDKs/transports, UI,
  flows, cloud/dev tools, quickstarts, and demos to make targeted local index
  expansion easier

### Changed

- **Locked developer setup** — install and update guidance now uses
  `uv sync --extra dev --group dev` instead of lockfile-bypassing editable
  install commands
- **Concurrent retrieval safety** — shared embedding, ChromaDB, and SQLite
  access is now serialized under load after the runtime stability benchmark
  reproduced a parallel `search_docs` crash
- **GitHub refresh safety** — repo slugs are validated before clone URLs are
  built, fetched refs are checked for taint before checkout, and tainted refs
  no longer advance local working trees
- **Least-privilege CI token scope** — the GitHub Actions workflow now declares
  explicit `GITHUB_TOKEN` permissions instead of relying on repository defaults

### Fixed

- Tainted upstream SHAs no longer overwrite last-known-good indexed metadata
  when refresh skips a compromised ref
- `llms-full.txt` is now streamed with a fixed size cap so an unexpectedly
  large upstream docs payload cannot grow refresh memory without bound
- Chroma product telemetry disabled via `NoOpProductTelemetryClient` —
  local dev tool should not phone home
- Integration tests now validate docs citations with parsed URL hostname checks
  instead of substring-style prefix matching
- Bumped `cryptography` to 46.0.6 to resolve upstream security advisory

## [0.0.10] - 2026-03-25

### Added

- **Chroma index recovery** — `refresh --reset-index` wipes and rebuilds the
  local index when persisted Chroma state is unhealthy. `IndexStore.close()`
  shuts down both backends cleanly. Benchmark health probe detects wedged
  vector state in ~16s instead of hanging for minutes.
- **`.pyi` stub file support** — `SourceIngester` now falls back to `.pyi`
  files at repo root when no Python packages exist in `src/`. Enables AST
  indexing of Rust+Python binding repos (e.g., `daily-co/daily-python`).
  `.pyi` files are only indexed by
  `SourceIngester` (not as code examples) to prevent duplicate chunks.
  Symlinks rejected + resolved-path containment checks at all file read sites.
- **Domain filtering for `search_examples`** — new `domain` filter param:
  `backend` (Python pipeline/bot code), `frontend` (JS/TS client code),
  `config` (YAML/TOML/JSON), `infra` (Docker/CI). Inferred from file path
  and language at ingestion time. Agents building Pipecat pipelines can use
  `search_examples(query="TTS", domain="backend")` to exclude frontend noise
- **Optional cross-encoder reranker** — `CrossEncoderReranker` service with
  lazy model loading, thread-safe inference via `asyncio.to_thread`, graceful
  offline degradation. Enabled by default; disable via
  `PIPECAT_HUB_RERANKER_ENABLED=0`
- **Result diversity** — repo/file diversity penalties and chunk-type
  preference for `search_api` (method > function > class > module)
- **Confidence guardrails** — `low_confidence: bool` on `EvidenceReport`,
  graduated count contribution, cross-tool suggestions, confidence floor
  with explicit `UnknownItem`

### Changed

- **`daily-co/daily-python` is now a default repo** — promoted from optional
  `PIPECAT_HUB_EXTRA_REPOS` to default sources. First-time refreshes now index
  Daily Python SDK (CallClient, EventHandler, 87 types) alongside Pipecat
  framework and examples.
- **Graduated staleness** — linear decay (max -0.10 at 365 days) replaces
  binary -0.05 at 90 days
- **UPPERCASE symbol detection** — TTS, STT, VAD, RTVI, LLM now receive
  symbol match boost
- **Dual-hit bonus** — +0.10 for chunks found by both vector AND keyword
- **Event loop** — `IndexStore` read methods offloaded to threads via
  `asyncio.to_thread` (no longer blocks event loop)

### Fixed

- FTS5 query sanitization strips double quotes to prevent syntax injection
- Cross-encoder model loading guarded by `threading.Lock` (no double-load)
- Model allowlist prevents loading untrusted models from HuggingFace

## [0.0.9] - 2026-03-23

### Added

- **Snippet enrichment for `get_code_snippet`** — `CodeSnippet` responses now
  populate three previously-empty fields from call-graph metadata:
  `dependency_notes` (per-method pipecat imports extracted from AST),
  `companion_snippets` (qualified method names called by this snippet), and
  `interface_expectations` (frame types yielded + base classes implemented).
  Computed at retrieval time — no index changes required
- **Per-method import extraction** — each method/function chunk now stores only
  the pipecat-internal imports that method actually references (via AST name
  resolution), not the entire module's import list. Fixes `dependency_notes`
  accuracy. Also improves `ApiHit.imports` precision for method/function chunks.
  Aliases (`import X as Y`) are preserved in import strings. Local imports
  inside function bodies correctly shadow module-level imports
- **Refresh summary table** — `refresh` command prints a per-source table
  showing status (updated/skipped/error), commit SHA, existing chunk count,
  and updated chunk count. Both columns sum to totals for at-a-glance
  verification
- `get_counts_by_repo()` on `FTSIndex` and `IndexStore` for pre-refresh
  chunk count snapshots
- **`AGENTS.md`** with Review Checklist for accepted design decisions

## [0.0.8] - 2026-03-17

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
