# Version-Aware Indexing & Deprecation Checking

**Status:** Not Started
**Priority:** Medium
**Branch:** `feature/version-aware-indexing`
**Created:** 2026-03-31
**Objective:** Track which pipecat version each repo/example targets, expose
deprecation checking as a first-class MCP tool, and enable version-aware
retrieval so users get compatible results.

## Context

The context hub indexes 70+ repos at HEAD. When a user asks "how do I use
DailyTransport?", we return the latest framework API + example code that may
have been written for an older or newer pipecat version. This creates a
mismatch:

- A user on pipecat `v0.0.95` gets examples written for `v0.0.98` that may
  use APIs that don't exist in their version
- A user on latest gets examples pinned to `v0.0.85` that use deprecated
  patterns
- There's no way to distinguish "this example uses the latest API" from
  "this example is 6 months stale"
- When the harness sees `from pipecat.services.grok import ...`, there's no
  way to immediately flag it as deprecated

### Current State

**What we track today:**
- `commit_sha` per chunk (which commit was indexed)
- `indexed_at` timestamp
- `repo` name
- Staleness score decay (-0.10 over 365 days)

**What we don't track:**
- Which pipecat version a repo targets
- Which APIs are deprecated in which version
- Version compatibility between framework and examples
- Whether a chunk's API still exists in the user's version

### Pipecat's Versioning Practices (research findings)

**Framework:**
- Dynamic versioning via `setuptools_scm` (git tags)
- Current release: v0.0.108
- CHANGELOG.md follows Keep a Changelog with `Added`, `Changed`,
  `Deprecated`, `Removed`, `Fixed` sections
- Breaking changes marked with warning symbols
- Migration guide at `docs.pipecat.ai/client/migration-guide`

**Deprecation mechanism:**
- `DeprecatedModuleProxy` redirects old import paths to new ones with
  `DeprecationWarning` — 37+ module-level deprecations (structured, parseable)
- `warnings.warn(DeprecationWarning)` for parameter/method deprecations
  (unstructured, scattered in source code)
- `.. deprecated:: 0.0.99` docstring directives (semi-structured)
- CHANGELOG `### Deprecated` and `### Removed` sections (human-authored prose)
- Old imports keep working until explicitly removed

**Example repo version pinning (observed patterns):**
| Pattern | Example | Count |
|---------|---------|-------|
| Exact pin `==0.0.98` | audio-recording-s3, nemotron | ~15 |
| Minimum `>=0.0.105` | pipecat-flows, pipecat-subagents | ~10 |
| Range `>=0.0.93,<1` | peekaboo | ~5 |
| No constraint | pipecat-quickstart | ~10 |
| Extras syntax | `pipecat-ai[daily,runner]>=0.0.105` | All of the above |

**Important: monorepo structure.**
`pipecat-ai/pipecat-examples` has `dependencies = []` at root. Each of
48+ example subdirectories has its own `pyproject.toml` with its own
pipecat-ai version pin. Version extraction must be per-example-directory,
not per-repo.

**Framework examples** (`pipecat-ai/pipecat/examples/`) have no dependency
files. They implicitly target the framework's own version.

**No `setup.py`/`setup.cfg`** found in any of the 73 indexed repos. All use
`pyproject.toml` or `requirements.txt`.

**TypeScript SDKs:**
- Use caret ranges (`^1.7.0`) allowing minor/patch updates
- Transport packages versioned separately but closely tracked

**Documentation:**
- Single docs site (always latest, no version selector)
- No versioned doc variants

### Review Findings (2026-04-01)

Plan review identified 1 critical and 6 important gaps. All incorporated
into the revised plan below.

**Critical — monorepo version extraction:** `pipecat-examples` requires
per-example-directory extraction, not per-repo. Each subdir has its own
`pyproject.toml`.

**Important — PEP 508 extras syntax:** Every real dependency uses
`pipecat-ai[daily,runner]>=0.0.105`. Must parse extras correctly.

**Important — `vector.py` metadata allowlist:** ChromaDB persistence uses
an explicit allowlist in `_record_to_metadata()`. New metadata keys not
added here are silently dropped.

**Important — scoring penalty too aggressive:** `-0.15` is 15% of the
score range (0-1). Start with `-0.05`, make tunable.

**Important — user version detection:** MCP server is a separate stdio
process. Auto-detection can't work. The harness should pass version as
a tool parameter instead.

**Important — deprecation scope:** `DeprecatedModuleProxy` only covers
module renames. Parameter/method deprecations via `warnings.warn()` and
docstring directives are unstructured. Scope Phase 1b to structured
sources only.

## Requirements

### Phase 1a: Version Extraction (metadata only)

1. **Extract pipecat version from each example/repo** — parse
   `pyproject.toml` and `requirements.txt` for `pipecat-ai` version
   constraints, handling PEP 508 extras syntax correctly
2. **Per-example-directory extraction** — for monorepos like
   `pipecat-examples`, walk from each example directory upward to find
   the nearest dependency file
3. **Framework repo special case** — chunks from `pipecat-ai/pipecat`
   examples get version derived from the repo's latest git tag
4. **Store as chunk metadata** — `pipecat_version_pin` field
5. **Persist to ChromaDB** — add to `_record_to_metadata()` allowlist in
   `vector.py`
6. **Surface in retrieval results** — add optional `pipecat_version_pin`
   field to `ExampleHit`, `ApiHit`, `CodeSnippet` output models
7. **No filtering or scoring changes** — purely additive metadata

### Phase 1b: Deprecation Check Tool (new MCP tool)

1. **Build deprecation map at refresh time** — parse `DeprecatedModuleProxy`
   usage from pipecat source + CHANGELOG `Deprecated`/`Removed` sections
2. **Expose `check_deprecation` MCP tool:**
   ```
   check_deprecation(symbol="pipecat.services.grok.llm")
   → {deprecated: true, replacement: "pipecat.services.xai.llm",
      deprecated_in: "0.0.100", removed_in: null}

   check_deprecation(symbol="pipecat.services.grok")
   → {deprecated: true, replacement: "pipecat.services.xai",
      deprecated_in: "0.0.100", removed_in: null}

   check_deprecation(symbol="DailyTransport")
   → {deprecated: false}
   ```
3. **MCP server instructions** — tell Claude: "When you see pipecat imports,
   check `check_deprecation` for deprecated APIs before recommending them"
4. **Deprecation scope for Phase 1b (structured sources only):**
   - `DeprecatedModuleProxy` usage in `__init__.py` files → old/new path
   - CHANGELOG.md `### Deprecated` → version + description (best-effort)
   - CHANGELOG.md `### Removed` → version + description (best-effort)
   - **Deferred:** inline `warnings.warn()` and docstring `.. deprecated::`
     (requires AST analysis, scope for Phase 3+)

### Phase 2: Version-Aware Retrieval

1. **Harness passes version as tool parameter** — the coding harness
   (Claude Code, Cursor) knows the user's `pyproject.toml` and passes
   the pipecat version on tool calls:
   ```
   search_examples("TTS pipeline", pipecat_version="0.0.95")
   search_api("DailyTransport", pipecat_version="0.0.95")
   ```
2. **Version-aware scoring** — boost compatible chunks, penalize
   incompatible ones (default penalty: `-0.05`, tunable)
3. **Compatibility annotations** — add `version_compatibility` field:
   `"compatible" | "newer_required" | "deprecated" | "unknown"`
4. **Opt-in filter** — allow excluding results targeting versions newer
   than the user's (`version_filter: "compatible_only"`)

### Phase 3: Enhanced Deprecation Detection (stretch)

1. **AST-based deprecation detection** — parse `warnings.warn(
   DeprecationWarning)` calls in pipecat source for parameter/method
   deprecations
2. **Docstring deprecation parsing** — extract `.. deprecated:: 0.0.99`
   directives from method docstrings
3. **Annotate example chunks** — cross-reference example imports against
   deprecation map, flag affected chunks

### Phase 4: Historical Version Indexing (stretch)

1. **Index specific git tags** — allow indexing `pipecat-ai/pipecat` at a
   specific tag (e.g., `v0.0.95`) instead of HEAD
2. **Config:** `PIPECAT_HUB_FRAMEWORK_VERSION=v0.0.95` env var or
   `refresh --framework-version v0.0.95` CLI flag
3. **Multi-version API surface** — users pinned to v0.0.95 get API docs
   from that version, not HEAD
4. **Storage cost:** ~3x source chunk count per additional version indexed

## Implementation Checklist

### Phase 1a: Version Extraction

- [ ] Add `_extract_pipecat_version(path: Path) -> str | None` helper
      in `github_ingest.py`:
      - Parse `pyproject.toml` `[project].dependencies` for `pipecat-ai`
      - Parse `requirements.txt` for `pipecat-ai` line
      - Handle PEP 508 extras syntax (`pipecat-ai[daily,runner]>=0.0.105`)
        using `packaging.Requirement` or regex strip
      - Walk upward from example directory to find nearest dependency file
      - For `pipecat-ai/pipecat` examples, derive from git tag
      - Skip `setup.py`/`setup.cfg` (none exist in indexed repos)
- [ ] Call version extraction per-example-directory in `_build_chunk_metadata()`
- [ ] Store `pipecat_version_pin` in chunk metadata during ingestion
- [ ] Add `pipecat_version_pin` to `_record_to_metadata()` allowlist in
      `vector.py` for ChromaDB persistence
- [ ] Add optional `pipecat_version_pin: str | None` field to `ExampleHit`,
      `ApiHit`, `CodeSnippet` in `types.py` (default `None` for backward compat)
- [ ] Surface in retrieval results via `hybrid.py`
- [ ] Handle missing version data gracefully (default `None`, no penalty)
- [ ] Document: `refresh --force` needed after upgrade to populate version pins
- [ ] Unit tests for version extraction:
      - Exact pin `==0.0.98`
      - Minimum `>=0.0.105`
      - Range `>=0.0.93,<1`
      - Extras syntax `pipecat-ai[daily,runner]>=0.0.105`
      - No version constraint `pipecat-ai[webrtc,daily]`
      - TypeScript caret range `^1.7.0`
      - Missing dependency file (returns `None`)
      - Monorepo per-directory extraction
      - Framework repo examples (derive from git tag)
- [ ] Bump `_SERVER_VERSION` (minor schema addition)

### Phase 1b: Deprecation Check Tool

- [ ] Create `deprecation_map.py` in `services/ingest/`:
      - Parse `DeprecatedModuleProxy` usage from cloned pipecat source
      - Parse CHANGELOG.md `### Deprecated` and `### Removed` sections
        (best-effort, seed manually for known deprecations)
      - Store as `DeprecationMap` dataclass (dict of `DeprecationEntry`)
      - Rebuild on each `refresh`
- [ ] Create `check_deprecation` MCP tool handler in `server/tools/`:
      - Input: `symbol: str` (module path, class name, or method name)
      - Output: `{deprecated: bool, replacement: str | None,
        deprecated_in: str | None, removed_in: str | None, note: str | None}`
      - Fuzzy matching: `pipecat.services.grok` matches `pipecat.services.grok.llm`
- [ ] Register tool in `main.py` tool registry
- [ ] Update `_SERVER_INSTRUCTIONS` to tell Claude to use `check_deprecation`
      when it sees pipecat imports
- [ ] Persist deprecation map to disk (rebuild on refresh, load on serve)
- [ ] Unit tests for deprecation map parsing:
      - `DeprecatedModuleProxy` extraction
      - CHANGELOG `### Deprecated` section parsing
      - CHANGELOG `### Removed` section parsing
      - Fuzzy symbol matching
- [ ] MCP smoke test: `check_deprecation("pipecat.services.grok.llm")`
      returns `{deprecated: true, replacement: "pipecat.services.xai.llm"}`

### Phase 2: Version-Aware Retrieval

- [ ] Add optional `pipecat_version: str | None` parameter to
      `SearchExamplesInput`, `SearchApiInput`, `GetCodeSnippetInput`
- [ ] Implement version comparison logic (PEP 440 for Python,
      semver for npm)
- [ ] Version-aware scoring in `rerank.py`:
      - Default penalty: `-0.05` for incompatible versions (tunable)
      - A/B test: highly relevant older example still ranks above
        irrelevant newer one
- [ ] Add `version_compatibility` field to result models
- [ ] Opt-in `version_filter` parameter on search tools
- [ ] Unit tests for version comparison and scoring
- [ ] MCP smoke tests with version parameter

### Phase 3: Enhanced Deprecation Detection (stretch)

- [ ] AST-based `warnings.warn(DeprecationWarning)` parsing
- [ ] Docstring `.. deprecated::` directive parsing
- [ ] Cross-reference example imports against full deprecation map
- [ ] Add `deprecated_apis` metadata field to affected chunks

### Phase 4: Historical Version Indexing (stretch)

- [ ] `PIPECAT_HUB_FRAMEWORK_VERSION` env var / `--framework-version` CLI
- [ ] Tag-based checkout in `GitHubRepoIngester.clone_or_fetch()`
- [ ] Separate index partitions per version
- [ ] Performance benchmarks for multi-version index

## Technical Specifications

### Version Extraction (Phase 1a)

**Parsing strategy:**
1. `pyproject.toml` → `[project].dependencies` → find entry matching
   `pipecat-ai` (strip extras with `packaging.Requirement` or regex
   `pipecat-ai\[.*?\]` before version extraction)
2. `requirements.txt` → line matching `pipecat-ai` (same extras handling)
3. `package.json` → `dependencies` / `peerDependencies` →
   `@pipecat-ai/client-js`
4. No `setup.py`/`setup.cfg` — none exist in indexed repos

**Monorepo handling:**
```python
def _extract_pipecat_version(example_dir: Path, repo_root: Path) -> str | None:
    """Walk upward from example_dir to repo_root looking for dependency files."""
    current = example_dir
    while current != repo_root.parent:
        for filename in ("pyproject.toml", "requirements.txt"):
            dep_file = current / filename
            if dep_file.is_file():
                version = _parse_pipecat_version_from(dep_file)
                if version is not None:
                    return version
        current = current.parent
    return None
```

**Framework repo special case:**
For chunks from `pipecat-ai/pipecat`, derive version from the repo's
HEAD commit tag (via `git describe --tags --abbrev=0`).

### Deprecation Map (Phase 1b)

```python
@dataclass
class DeprecationEntry:
    old_path: str           # e.g., "pipecat.services.grok.llm"
    new_path: str | None    # e.g., "pipecat.services.xai.llm"
    deprecated_in: str | None  # version string, e.g., "0.0.100"
    removed_in: str | None  # version if removed, else None
    note: str = ""          # human-readable description

@dataclass
class DeprecationMap:
    entries: dict[str, DeprecationEntry]  # keyed by old_path

    def check(self, symbol: str) -> DeprecationEntry | None:
        """Fuzzy match: 'pipecat.services.grok' matches
        'pipecat.services.grok.llm'."""
        if symbol in self.entries:
            return self.entries[symbol]
        # Prefix match
        for key, entry in self.entries.items():
            if key.startswith(symbol + ".") or symbol.startswith(key + "."):
                return entry
        return None
```

**Sources (structured, Phase 1b):**
1. `DeprecatedModuleProxy` usage in `__init__.py` files → old/new module path
2. CHANGELOG.md `### Deprecated` → version + description (best-effort prose parsing)
3. CHANGELOG.md `### Removed` → version + description

**Sources (unstructured, deferred to Phase 3):**
4. `warnings.warn(DeprecationWarning, ...)` calls in source code
5. `.. deprecated:: 0.0.99` docstring directives

### Version-Aware Scoring (Phase 2)

```python
# Harness passes user version as tool parameter
user_version = "0.0.95"  # from tool call, not env var
chunk_version_pin = ">=0.0.105"  # from chunk metadata

# Version comparison using packaging.Version
from packaging.version import Version
from packaging.specifiers import SpecifierSet

user_v = Version(user_version)
spec = SpecifierSet(chunk_version_pin)

if user_v not in spec:
    # Chunk requires newer → small penalty, annotate
    score_adjustment = -0.05  # tunable, not -0.15
    compatibility = "newer_required"
else:
    score_adjustment = 0
    compatibility = "compatible"
```

### MCP Tool: `check_deprecation`

```python
class CheckDeprecationInput(BaseModel):
    symbol: str = Field(
        max_length=256,
        description="Module path, class name, or method to check. "
        "E.g., 'pipecat.services.grok.llm' or 'TTSService.add_word_timestamps'.",
    )

class CheckDeprecationOutput(BaseModel):
    deprecated: bool
    replacement: str | None = None
    deprecated_in: str | None = None
    removed_in: str | None = None
    note: str | None = None
```

### Files to Modify

| Phase | File | Changes |
|-------|------|---------|
| 1a | `github_ingest.py` | `_extract_pipecat_version()` helper, call per-example-dir |
| 1a | `vector.py` | Add `pipecat_version_pin` to `_record_to_metadata()` allowlist |
| 1a | `types.py` | `pipecat_version_pin` on `ExampleHit`, `ApiHit`, `CodeSnippet` |
| 1a | `hybrid.py` | Surface version in results |
| 1a | `main.py` | Bump `_SERVER_VERSION` |
| 1b | New: `services/ingest/deprecation_map.py` | Parse deprecations |
| 1b | New: `server/tools/check_deprecation.py` | Tool handler |
| 1b | `main.py` | Register `check_deprecation` tool, update instructions |
| 1b | `cli.py` | Build deprecation map during `refresh` |
| 2 | `types.py` | `pipecat_version` param on search input models |
| 2 | `rerank.py` | Version-aware scoring adjustments |
| 2 | `hybrid.py` | `version_compatibility` field, version filter |

### Integration Seams

| Seam | Contract |
|------|----------|
| `_extract_pipecat_version(path, repo_root)` | Returns version constraint string or None. Called per-example-dir during ingest. |
| `DeprecationMap.check(symbol)` | Returns DeprecationEntry or None. Loaded from disk at server startup. |
| `check_deprecation` MCP tool | Takes symbol string, returns deprecation status. Available to harness without version context. |
| `pipecat_version` tool parameter | Optional param on search tools. Harness reads user's pyproject.toml and passes version. |
| `_record_to_metadata()` in vector.py | Must include `pipecat_version_pin` in allowlist or field is silently dropped. |

## Open Questions

1. **CHANGELOG parsing reliability** — prose sections are fragile to parse.
   Should we seed the deprecation map manually for known deprecations and
   use CHANGELOG parsing as a best-effort supplement?
2. **Should `check_deprecation` also check class/method names?** — Phase 1b
   covers module paths. Expanding to class/method names requires AST analysis
   of `warnings.warn()` calls (Phase 3).
3. **Version penalty tuning** — -0.05 is conservative. Should this be
   configurable per-user or fixed? Start fixed, add config if needed.
4. **Multi-version indexing cost (Phase 4)** — indexing 3 versions would
   ~3x the source chunk count (~15K → ~45K). Worth the storage and
   retrieval performance cost?
5. **TS SDK version tracking** — `@pipecat-ai/client-js` uses `^1.7.0`
   (caret ranges). Should this be a separate field from the Python version
   pin, or unified?

## Review Focus

- **Version extraction reliability** — PEP 508 extras syntax, monorepo
  per-directory parsing, framework repo special case
- **Deprecation map completeness** — Phase 1b covers module renames only.
  Verify this covers the most user-visible deprecation scenarios.
- **Scoring balance** — -0.05 penalty should not overwhelm relevance.
  A highly relevant example from v0.0.95 beats an irrelevant one from HEAD.
- **`check_deprecation` tool design** — fuzzy matching, response format,
  harness integration via MCP server instructions
- **Backward compatibility** — new optional fields on output models must
  default to None. Existing MCP clients should not break.

## Acceptance Criteria

- [ ] `search_examples` results include `pipecat_version_pin` when available
- [ ] `check_deprecation("pipecat.services.grok.llm")` returns deprecation
      info with replacement path
- [ ] `check_deprecation("DailyTransport")` returns `{deprecated: false}`
- [ ] MCP server instructions mention `check_deprecation` for import checking
- [ ] Examples using deprecated modules are discoverable via the tool
- [ ] Existing 29 smoke tests still pass (no regressions)
- [ ] `refresh --force` populates version pins on all chunks
- [ ] Version info defaults to `None` gracefully for chunks without it
