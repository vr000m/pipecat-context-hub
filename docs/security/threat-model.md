# MCP Server Threat Model

## Scope

This document covers the Pipecat Context Hub MCP server and its refresh/runtime
pipeline:

- `pipecat-context-hub refresh`
- `pipecat-context-hub serve`
- local Chroma/SQLite persistence under `storage.data_dir`
- remote documentation ingestion
- remote GitHub repository ingestion
- local model loading for embeddings and optional reranking

Out of scope for this document unless they directly affect the server runtime:

- dashboard static assets and visualization scripts
- client configuration templates
- downstream editor/agent behavior after MCP responses leave this process

## Security Goals

- Remote content must remain data-only. Fetching docs, repositories, or model
  artifacts must not create an execution path for untrusted code.
- Filesystem writes and deletes must remain scoped to the configured local data
  directory.
- A compromised or suspicious upstream repository or ref must be skippable by
  local policy without forcing users to ingest it.
- Long-lived server resources must not accumulate across repeated refresh or
  serve cycles.
- Developer setup and review workflows must be reproducible from the repo's
  locked dependency state.

## Assets To Protect

- local developer machine integrity
- local index data under `~/.pipecat-context-hub/` or configured override
- repo metadata and cached commit SHAs
- MCP responses returned to local coding agents
- CPU, memory, threads, file descriptors, and disk usage on the developer host

## Trust Boundaries

### Boundary 1: Remote docs to local index

Path:

- `docs.pipecat.ai/llms-full.txt`
- [`docs_crawler.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/ingest/docs_crawler.py)
- local chunking and index writes

Risks:

- malicious content attempting prompt or agent injection
- unexpectedly large responses causing memory or disk pressure
- malformed markdown or tags triggering parser bugs

Current controls:

- fetched content is treated as text and chunked, not executed
- HTTP client lifetime is explicit and closable
- docs are stored in the local index, not imported as code

Follow-up checks:

- verify size and failure handling under very large or malformed payloads
- consider explicit maximum response-size guardrails if real-world corpus growth
  becomes a pressure point

### Boundary 2: Remote GitHub repos to local clone and index

Path:

- configured default repos plus `PIPECAT_HUB_EXTRA_REPOS`
- [`github_ingest.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/ingest/github_ingest.py)
- [`source_ingest.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/ingest/source_ingest.py)

Risks:

- path traversal or clone-directory escape
- tainted upstream commits or releases
- symlink escape during file reads
- indexing large or adversarial repos that stress memory or storage

Current controls:

- repo slug sanitization and resolved-path containment for clones
- source-ingest guards against symlinks and resolved paths outside the clone
- tainted upstream repos can be skipped via `PIPECAT_HUB_TAINTED_REPOS`
- tainted upstream refs can be skipped via `PIPECAT_HUB_TAINTED_REFS`
- fetched refs are now evaluated before local checkout is updated

Residual risks:

- refresh still trusts configured repo owners and the Git transport itself
- extra repos from environment config widen the trust boundary intentionally

Operational policy:

- review upstream release notes or security advisories before removing a repo or
  ref from the taint list
- prefer denylisting an exact tag or commit when possible; denylist the whole
  repo when compromise scope is unclear

### Boundary 3: Model download and local inference

Path:

- `sentence-transformers` model downloads and cache
- embedding model load
- optional cross-encoder reranker load

Risks:

- compromised model artifacts
- unexpected memory growth during repeated load/use cycles
- arbitrary model names creating unexpected downloads

Current controls:

- cross-encoder models are allowlisted
- embedding and reranker load locally on CPU
- reranker can be disabled by config or env

Residual risks:

- embedding model name is configurable and not currently allowlisted
- no soak test yet proves stable memory behavior across repeated cycles

### Boundary 4: Local persistence and destructive operations

Path:

- `storage.data_dir`
- Chroma persistence
- SQLite metadata store
- reset/delete operations in refresh and recovery flows

Risks:

- deletion outside intended storage root
- index divergence between vector and keyword backends
- stale metadata causing unsafe skips
- resource leaks on shutdown

Current controls:

- storage paths are derived from a dedicated config root
- reset/rebuild flow is explicit through CLI flags
- store close/reset lifecycle exists
- read-path search calls are offloaded to threads to avoid blocking the event loop

Follow-up checks:

- add soak testing for repeated refresh/serve cycles
- add explicit metrics or harness output for RSS, threads, and file descriptors

### Boundary 5: MCP stdio server to local agent

Path:

- [`server/main.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/server/main.py)
- [`server/transport.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/server/transport.py)
- local IDE or agent client over stdio

Risks:

- prompt injection through retrieved content
- malformed output breaking clients
- concurrent request behavior causing lifecycle or state bugs

Current controls:

- server exposes explicit MCP tools and typed inputs
- tool descriptions instruct agents to treat the hub as retrieval context, not
  executable content
- read paths use async thread offload for blocking backends

Residual risks:

- response content is still untrusted input to the downstream agent
- no dedicated concurrency soak test exists yet

## Assumptions

- the developer intentionally trusts the local Python runtime and package manager
- default upstream repos are maintained by trusted organizations unless marked
  tainted locally
- network compromise and package registry compromise are outside the scope of
  this application alone and must be mitigated partly through dependency and
  review gates

## Required Review Gates

- quality: `ruff`, `mypy`, `pytest`
- dependency audit: `pip-audit`
- static security scan: `bandit`
- SBOM generation: `cyclonedx-py`

These gates do not replace manual review. They reduce obvious regressions and
provide repeatable evidence during release review.

Current exception:

- `pip-audit` ignores `CVE-2026-4539` for `pygments` because the advisory does
  not currently surface a fixed PyPI version. This accepted risk is tracked in
  [AGENTS.md](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/AGENTS.md) and
  should be revisited as soon as upstream publishes a fix.

## Residual Risks

- no full soak/leak harness yet
- no immutable upstream pinning yet beyond local taint denylisting
- no repo-local automation yet for upstream advisory ingestion; operators still
  need to decide which repos or refs to taint

## Next Hardening Steps

- add CI workflows for the review gates
- add soak/leak validation for repeated `refresh` and concurrent `serve` calls
- decide whether to add optional ref pinning in addition to denylisting
