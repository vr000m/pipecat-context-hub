# Agents Guide

Project conventions and decisions for AI coding agents working on this codebase.

## Review Checklist

Findings that have been reviewed and deliberately accepted. Do not re-flag these
in future reviews unless the underlying circumstances change.

- **[Architecture] won't-fix**: CodeSnippet enrichment fields (`dependency_notes`, `companion_snippets`, `interface_expectations`) use different names than ApiHit's raw fields (`imports`, `calls`, `yields`, `base_classes`). This is intentional — ApiHit is a raw API surface for exploration, CodeSnippet is an agent-facing enriched view with qualified names and human-readable formatting. Revisit if a third tool type needs the same data. (2026-03-22)

- **[Architecture] won't-fix**: `get_code_snippet` enrichment logic (line_sliced detection, module_overview guard, metadata mapping) is inline in the method rather than extracted into helpers. The method is ~50 lines with clear comments. Extract helpers if enrichment gains more suppression conditions or new enrichment fields. (2026-03-22)

- **[Security] won't-fix**: Chunk metadata values (class_name, calls, yields, etc.) flow unsanitized into MCP JSON-RPC responses. The AST ingester constrains these to valid Python identifiers, so there is no executable sink. Add input validation if non-AST ingestion sources (e.g., user-supplied metadata, external APIs) are introduced. (2026-03-22)

- **[Architecture] won't-fix**: `ApiHit.imports` has mixed precision by chunk type — per-method for method/function chunks, module-level pipecat imports for class_overview, full imports (including stdlib) for module_overview. This is a deliberate layering: `source_ingest._build_chunks` populates each chunk type differently, and `hybrid.py` passes the field through unchanged. The `ApiHit.imports` description documents the per-chunk-type semantics. Revisit only if a consumer needs uniform precision across chunk types. (2026-03-23)

- **[Logic] won't-fix**: Confidence scores are optimistic on weak `search_examples` results — noisy keyword matches from large repos (e.g., gradient-bang frontend files) score high via RRF + dual-hit bonus, driving confidence to ~0.95 even when results are semantically irrelevant. This is a retrieval quality issue, not a confidence calibration bug. The cross-encoder (Phase 1, disabled by default) directly addresses this by scoring query-result *pairs* for semantic relevance. Without cross-encoder, confidence reflects score distribution, not true relevance. Follow-up: example corpus weighting / repo scoring to reduce noise from non-pipeline code. (2026-03-24)

- **[Security] accepted-risk**: `pip-audit` reports `pygments 2.19.2` for `CVE-2026-4539`, but as of March 25, 2026 it does not provide a fixed PyPI version. The package is currently present transitively via `rich`/`pytest`, so the repo-local audit gate ignores this single CVE with an explicit `--ignore-vuln` entry rather than disabling `pip-audit` more broadly. Revisit as soon as upstream publishes a fixed release or the advisory guidance changes. (2026-03-25)
