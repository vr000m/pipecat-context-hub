# Agents Guide

Project conventions and decisions for AI coding agents working on this codebase.

## Review Checklist

Findings that have been reviewed and deliberately accepted. Do not re-flag these
in future reviews unless the underlying circumstances change.

- **[Architecture] won't-fix**: CodeSnippet enrichment fields (`dependency_notes`, `companion_snippets`, `interface_expectations`) use different names than ApiHit's raw fields (`imports`, `calls`, `yields`, `base_classes`). This is intentional — ApiHit is a raw API surface for exploration, CodeSnippet is an agent-facing enriched view with qualified names and human-readable formatting. Revisit if a third tool type needs the same data. (2026-03-22)

- **[Architecture] won't-fix**: `get_code_snippet` enrichment logic (line_sliced detection, module_overview guard, metadata mapping) is inline in the method rather than extracted into helpers. The method is ~50 lines with clear comments. Extract helpers if enrichment gains more suppression conditions or new enrichment fields. (2026-03-22)

- **[Security] won't-fix**: Chunk metadata values (class_name, calls, yields, etc.) flow unsanitized into MCP JSON-RPC responses. The AST ingester constrains these to valid Python identifiers, so there is no executable sink. Add input validation if non-AST ingestion sources (e.g., user-supplied metadata, external APIs) are introduced. (2026-03-22)
