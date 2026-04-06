# Version-Aware Indexing & Deprecation Checking

**Status:** Phase 1a + 1b + 2 Complete
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
- Dynamic versioning via `setuptools_scm` (git tags) — no static
  `version = "X.Y.Z"` in pyproject.toml. Version derived from `git describe`
  at build time.
- Current release: v0.0.108
- CHANGELOG.md follows Keep a Changelog with `Added`, `Changed`,
  `Deprecated`, `Removed`, `Fixed` sections
- Breaking changes marked with warning symbols
- Migration guide at `docs.pipecat.ai/client/migration-guide`

**Deprecation mechanism:**
- `DeprecatedModuleProxy` redirects old import paths to new ones with
  `DeprecationWarning` — 37 module-level deprecations (structured, parseable)
  - Some use bracket-expansion: `"cartesia.[stt,tts]"` and
    `"[ai_service,image_service,llm_service,...]"` — parser must expand these
- `warnings.warn(DeprecationWarning)` for parameter/method deprecations
  (20+ instances, unstructured, scattered in source code — deferred to Phase 3)
- `.. deprecated:: 0.0.99` docstring directives (semi-structured — deferred)
- CHANGELOG `### Deprecated` and `### Removed` sections (human-authored prose,
  best-effort parsing as supplement to DeprecatedModuleProxy)
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
not per-repo. The walk-upward strategy must search for `pipecat-ai` in
the dependency list — not just find the nearest `pyproject.toml` (which
may have `dependencies = []`).

**Framework examples** (`pipecat-ai/pipecat/examples/`) have no dependency
files. They implicitly target the framework's own version (derived from
`git describe --tags --abbrev=0`).

**10+ repos use `requirements.txt`** (spotify-assistant-demo,
gemini-webrtc-web-simple, smart-turn, HeyGen demo, etc.). Parse with
`packaging.Requirement` which handles PEP 508 for both pyproject.toml
and requirements.txt lines.

**No `setup.py`/`setup.cfg`** found in any of the 73 indexed repos.

**TypeScript SDKs:**
- Use caret ranges (`^1.7.0`) allowing minor/patch updates
- Transport packages versioned separately but closely tracked

**Documentation:**
- Single docs site (always latest, no version selector)
- No versioned doc variants

### Review Findings (2026-04-01, 2026-04-02)

Two rounds of plan review identified 1 critical and 10+ important gaps.
All incorporated into the revised plan below.

**Critical — monorepo version extraction:** per-example-directory, not
per-repo. Walk-upward must check for pipecat-ai in deps, not just find
nearest pyproject.toml.

**Important — `_build_chunk_metadata()` has no filesystem access:**
Version extraction happens at `_ingest_repo` level, passed down as param.

**Important — `_metadata_to_record_fields()` also needs updating:**
Symmetric round-trip function in `vector.py` must match allowlist.

**Important — `check_deprecation` dispatch pattern:** New tool needs
`DeprecationMap`, not `Retriever` or `IndexStore`. Make accessible via
`HybridRetriever` attribute to avoid special-case dispatch.

**Important — `packaging` not declared as dependency:** Add to
`pyproject.toml` explicitly.

**Important — bracket-expansion in DeprecatedModuleProxy:** Parser must
handle `"cartesia.[stt,tts]"` and `"[ai_service,image_service,...]"`.

**Important — combined staleness + version penalty:** Cap combined
penalty or make mutually exclusive.

**Minor — Retriever protocol unaffected:** New optional fields with
`default=None` on Pydantic input models are backward-compatible.

**Minor — deprecation map cache invalidation:** Rebuild on each `refresh`.

## Requirements

### Phase 1a: Version Extraction (metadata only)

1. **Extract pipecat version from each example/repo** — parse
   `pyproject.toml` and `requirements.txt` for `pipecat-ai` version
   constraints using `packaging.Requirement` (handles PEP 508 extras)
2. **Per-example-directory extraction** — walk upward from each example
   directory, searching for `pipecat-ai` in the dependency list (not just
   finding nearest `pyproject.toml` — root may have `dependencies = []`)
3. **Framework repo special case** — chunks from `pipecat-ai/pipecat`
   examples get version derived from `git describe --tags --abbrev=0`
   (framework uses `setuptools_scm`, no static version in pyproject.toml)
4. **Store as chunk metadata** — `pipecat_version_pin` field
5. **Persist to ChromaDB** — add to both `_record_to_metadata()` AND
   `_metadata_to_record_fields()` in `vector.py` (both must be updated
   or the field is lost on round-trip)
6. **Surface in retrieval results** — add optional `pipecat_version_pin`
   field to `ExampleHit`, `ApiHit`, `CodeSnippet` output models
   (`default=None` for backward compatibility — Retriever protocol
   unaffected since new fields are on Pydantic input/output models)
7. **No filtering or scoring changes** — purely additive metadata
8. **Add `packaging` to `pyproject.toml` dependencies** — currently
   transitive only, must be declared explicitly

### Phase 1b: Deprecation Check Tool (new MCP tool)

1. **Build deprecation map at refresh time** — parse `DeprecatedModuleProxy`
   usage from pipecat source + CHANGELOG `Deprecated`/`Removed` sections
2. **Handle bracket-expansion syntax** — `"cartesia.[stt,tts]"` expands to
   `["pipecat.services.cartesia.stt", "pipecat.services.cartesia.tts"]` and
   `"[ai_service,image_service,...]"` expands to individual module paths
3. **Expose `check_deprecation` MCP tool:**
   ```
   check_deprecation(symbol="pipecat.services.grok.llm")
   → {deprecated: true, replacement: "pipecat.services.xai.llm",
      deprecated_in: "0.0.100", removed_in: null}

   check_deprecation(symbol="DailyTransport")
   → {deprecated: false}
   ```
4. **MCP server instructions** — tell Claude: "When you see pipecat imports,
   check `check_deprecation` for deprecated APIs before recommending them"
5. **Deprecation scope for Phase 1b (structured sources only):**
   - `DeprecatedModuleProxy` usage in `__init__.py` files → old/new path
     (primary, reliable source — includes bracket-expansion)
   - CHANGELOG.md `### Deprecated` / `### Removed` → version + description
     (best-effort supplement, seed manually for known entries)
   - **Explicitly deferred:** inline `warnings.warn()` (20+ instances) and
     docstring `.. deprecated::` directives — requires AST analysis (Phase 3)
6. **Rebuild deprecation map on each `refresh`** — store commit SHA
   alongside map for staleness detection
7. **Tool dispatch:** make `DeprecationMap` accessible as an attribute on
   `HybridRetriever` (avoids adding another special-case `if name == ...`
   branch in `call_tool()`)

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
3. **Combined penalty cap** — staleness + version penalty capped at
   `-0.10` total (prevents old + incompatible examples from being
   penalized twice for correlated "oldness")
4. **Compatibility annotations** — add `version_compatibility` field:
   `"compatible" | "newer_required" | "deprecated" | "unknown"`
5. **Opt-in filter** — allow excluding results targeting versions newer
   than the user's (`version_filter: "compatible_only"`)

### Phase 3: Enhanced Deprecation Detection (stretch)

1. **AST-based deprecation detection** — parse `warnings.warn(
   DeprecationWarning)` calls in pipecat source for parameter/method
   deprecations (20+ instances in base_input.py, task.py, etc.)
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

- [x] Add `packaging>=21.0,<27.0` to `[project].dependencies` in
      `pyproject.toml` (currently transitive only — must be explicit)
- [x] Add `_extract_pipecat_version(example_dir: Path, repo_root: Path)
      -> str | None` helper in `github_ingest.py`:
      - Walk upward from example_dir to repo_root
      - At each level, check pyproject.toml and requirements.txt for a
        `pipecat-ai` dependency (not just existence of the file)
      - Use `packaging.Requirement` to parse PEP 508 entries with extras
      - Extract version specifier as string (e.g., `">=0.0.105"`)
      - For `pipecat-ai/pipecat` repo, use `git describe --tags --abbrev=0`
      - For `package.json`, check `@pipecat-ai/client-js` in dependencies
- [x] Call version extraction at `_ingest_repo` level (around line 783),
      once per example directory, then pass result into
      `_build_chunk_metadata()` as a new `pipecat_version: str | None` param
- [x] Store `pipecat_version_pin` in chunk metadata during ingestion
- [x] Add `pipecat_version_pin` to BOTH `_record_to_metadata()` AND
      `_metadata_to_record_fields()` in `vector.py` (both must match or
      field is lost on ChromaDB round-trip)
- [x] Add optional `pipecat_version_pin: str | None` field to `ExampleHit`,
      `ApiHit`, `CodeSnippet` in `types.py` (default `None` — Retriever
      protocol unaffected, backward compatible)
- [x] Surface in retrieval results via `hybrid.py`
- [x] Handle missing version data gracefully (default `None`, no penalty)
- [x] Document: `refresh --force` needed after upgrade to populate version pins
- [x] Unit tests for version extraction:
      - Exact pin `==0.0.98`
      - Minimum `>=0.0.105`
      - Range `>=0.0.93,<1`
      - Extras syntax `pipecat-ai[daily,runner]>=0.0.105`
      - No version constraint `pipecat-ai[webrtc,daily]` (returns `None`)
      - TypeScript caret range `^1.7.0` from package.json
      - Missing dependency file (returns `None`)
      - Monorepo: root pyproject.toml has `dependencies = []`, subdir has pin
      - Framework repo examples: derive from git tag
      - requirements.txt format: `pipecat-ai[daily]>=0.0.100,<0.1`
- [x] Bump `_SERVER_VERSION` (minor schema addition)

### Phase 1b: Deprecation Check Tool

- [x] Create `deprecation_map.py` in `services/ingest/`:
      - Parse `DeprecatedModuleProxy` usage from cloned pipecat source
      - Handle bracket-expansion: `"cartesia.[stt,tts]"` → two entries,
        `"[ai_service,image_service,...]"` → individual module entries
      - Parse CHANGELOG.md `### Deprecated` and `### Removed` sections
        (best-effort supplement, seed manually for known deprecations)
      - Store as `DeprecationMap` dataclass (dict of `DeprecationEntry`)
      - Rebuild on each `refresh`, store pipecat commit SHA for staleness
      - Persist to disk (JSON) for loading at server startup
- [x] Create `check_deprecation` MCP tool handler in `server/tools/`:
      - Input: `symbol: str` (module path, class name, or method name)
      - Output: `{deprecated: bool, replacement: str | None,
        deprecated_in: str | None, removed_in: str | None, note: str | None}`
      - Fuzzy matching: `pipecat.services.grok` matches
        `pipecat.services.grok.llm`
- [x] Make `DeprecationMap` accessible via `HybridRetriever` attribute
      (avoids special-case dispatch in `call_tool()`)
- [x] Register tool in `main.py` tool registry
- [x] Update `_SERVER_INSTRUCTIONS` to tell Claude to use `check_deprecation`
      when it sees pipecat imports
- [x] Unit tests for deprecation map parsing:
      - `DeprecatedModuleProxy` extraction (standard format)
      - Bracket-expansion: `"cartesia.[stt,tts]"` → 2 entries
      - Bracket-expansion: `"[ai_service,image_service,...]"` → N entries
      - CHANGELOG `### Deprecated` section parsing
      - CHANGELOG `### Removed` section parsing
      - Fuzzy symbol matching (prefix, exact, partial)
- [x] MCP smoke test: `check_deprecation("pipecat.services.grok.llm")`
      returns `{deprecated: true, replacement: "pipecat.services.xai.llm"}`

### Phase 2: Version-Aware Retrieval

- [x] Add optional `pipecat_version: str | None` parameter to
      `SearchExamplesInput`, `SearchApiInput`, `GetCodeSnippetInput`
      (default `None` — Retriever protocol unaffected)
- [x] Implement version comparison logic using `packaging.Version` and
      `packaging.specifiers.SpecifierSet` (PEP 440 for Python)
- [x] Version-aware scoring in `rerank.py`:
      - Default penalty: `-0.05` for incompatible versions (tunable)
      - Combined cap: staleness + version penalty ≤ `-0.10` total
      - A/B test: highly relevant older example still ranks above
        irrelevant newer one
- [x] Add `version_compatibility` field to result models
- [x] Opt-in `version_filter` parameter on search tools
- [x] Unit tests for version comparison and scoring
- [x] MCP smoke tests with version parameter

### Phase 3: Enhanced Deprecation Detection (stretch)

- [ ] AST-based `warnings.warn(DeprecationWarning)` parsing (20+ instances)
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

**Parsing strategy using `packaging.Requirement`:**
```python
from packaging.requirements import Requirement

def _parse_pipecat_version_from_pyproject(path: Path) -> str | None:
    """Extract pipecat-ai version constraint from pyproject.toml."""
    import tomllib
    with open(path, "rb") as f:
        data = tomllib.load(f)
    deps = data.get("project", {}).get("dependencies", [])
    for dep_str in deps:
        req = Requirement(dep_str)
        if req.name == "pipecat-ai":
            return str(req.specifier) or None  # e.g., ">=0.0.105"
    return None

def _parse_pipecat_version_from_requirements(path: Path) -> str | None:
    """Extract pipecat-ai version constraint from requirements.txt."""
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            req = Requirement(line)
            if req.name == "pipecat-ai":
                return str(req.specifier) or None
        except Exception:
            continue
    return None
```

**Monorepo walk-upward (searches for pipecat-ai, not just any pyproject):**
```python
def _extract_pipecat_version(example_dir: Path, repo_root: Path) -> str | None:
    """Walk upward from example_dir looking for pipecat-ai dependency."""
    current = example_dir
    while current != repo_root.parent:
        # Check pyproject.toml
        pyproject = current / "pyproject.toml"
        if pyproject.is_file():
            version = _parse_pipecat_version_from_pyproject(pyproject)
            if version is not None:
                return version
        # Check requirements.txt
        reqs = current / "requirements.txt"
        if reqs.is_file():
            version = _parse_pipecat_version_from_requirements(reqs)
            if version is not None:
                return version
        current = current.parent
    return None
```

**Framework repo special case:**
```python
def _get_framework_version(repo_path: Path) -> str | None:
    """Get pipecat version from git tag (setuptools_scm repos)."""
    try:
        from git import Repo
        repo = Repo(str(repo_path))
        tag = repo.git.describe("--tags", "--abbrev=0")
        return tag.lstrip("v")  # "v0.0.108" → "0.0.108"
    except Exception:
        return None
```

**Callsite:** version extraction happens at `_ingest_repo` level (line ~783
in `github_ingest.py`), once per example directory. Result passed into
`_build_chunk_metadata()` as `pipecat_version: str | None` parameter.
`_build_chunk_metadata()` does NOT do filesystem I/O.

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
    pipecat_commit_sha: str = ""  # for staleness detection

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

**Bracket-expansion parsing:**
```python
def _expand_bracket_module(old_module: str, new_module: str) -> list[tuple[str, str]]:
    """Expand bracket syntax in DeprecatedModuleProxy args.

    "cartesia.[stt,tts]" → [("cartesia.stt", ...), ("cartesia.tts", ...)]
    "[ai_service,image_service]" → [("ai_service", ...), ("image_service", ...)]
    """
    import re
    match = re.search(r'\[([^\]]+)\]', new_module)
    if not match:
        return [(old_module, new_module)]
    parts = [p.strip() for p in match.group(1).split(",")]
    prefix = new_module[:match.start()]
    return [(old_module, prefix + p) for p in parts]
```

**Sources (structured, Phase 1b):**
1. `DeprecatedModuleProxy` usage in `__init__.py` files → old/new module path
   (including bracket-expansion)
2. CHANGELOG.md `### Deprecated` → version + description (best-effort)
3. CHANGELOG.md `### Removed` → version + description (best-effort)

**Sources (unstructured, deferred to Phase 3):**
4. `warnings.warn(DeprecationWarning, ...)` calls (20+ in source)
5. `.. deprecated:: 0.0.99` docstring directives

### Version-Aware Scoring (Phase 2)

```python
# Harness passes user version as tool parameter
user_version = "0.0.95"  # from tool call
chunk_version_pin = ">=0.0.105"  # from chunk metadata

from packaging.version import Version
from packaging.specifiers import SpecifierSet

user_v = Version(user_version)
spec = SpecifierSet(chunk_version_pin)

if user_v not in spec:
    score_adjustment = -0.05  # tunable
    compatibility = "newer_required"
else:
    score_adjustment = 0
    compatibility = "compatible"

# Combined cap: staleness + version ≤ -0.10
total_penalty = staleness_penalty + score_adjustment
if total_penalty < -0.10:
    score_adjustment = -0.10 - staleness_penalty
```

### MCP Tool: `check_deprecation`

```python
class CheckDeprecationInput(BaseModel):
    symbol: str = Field(
        max_length=256,
        description="Module path, class name, or method to check. "
        "E.g., 'pipecat.services.grok.llm' or 'DailyTransport'.",
    )

class CheckDeprecationOutput(BaseModel):
    deprecated: bool
    replacement: str | None = None
    deprecated_in: str | None = None
    removed_in: str | None = None
    note: str | None = None
```

**Dispatch:** `DeprecationMap` is an attribute on `HybridRetriever`
(loaded at startup from persisted JSON). The tool handler accesses it
via `retriever.deprecation_map.check(symbol)` — no special-case dispatch.

### Files to Modify

| Phase | File | Changes |
|-------|------|---------|
| 1a | `pyproject.toml` | Add `packaging>=21.0,<27.0` dependency |
| 1a | `github_ingest.py` | `_extract_pipecat_version()` helper, call at `_ingest_repo` level |
| 1a | `vector.py` | Add `pipecat_version_pin` to both `_record_to_metadata()` AND `_metadata_to_record_fields()` |
| 1a | `types.py` | `pipecat_version_pin` on `ExampleHit`, `ApiHit`, `CodeSnippet` |
| 1a | `hybrid.py` | Surface version in results |
| 1a | `main.py` | Bump `_SERVER_VERSION` |
| 1b | New: `services/ingest/deprecation_map.py` | Parse deprecations, bracket-expansion |
| 1b | New: `server/tools/check_deprecation.py` | Tool handler |
| 1b | `hybrid.py` / `retrieval/` | Add `deprecation_map` attribute to `HybridRetriever` |
| 1b | `main.py` | Register `check_deprecation` tool, update instructions |
| 1b | `cli.py` | Build + persist deprecation map during `refresh` |
| 2 | `types.py` | `pipecat_version` param on search input models |
| 2 | `rerank.py` | Version-aware scoring with combined penalty cap |
| 2 | `hybrid.py` | `version_compatibility` field, version filter |

### Integration Seams

| Seam | Contract |
|------|----------|
| `_extract_pipecat_version(example_dir, repo_root)` | Returns version constraint string or None. Called at `_ingest_repo` level per-example-dir. Does NOT run inside `_build_chunk_metadata()`. |
| `_build_chunk_metadata(..., pipecat_version)` | Receives pre-extracted version as a new string parameter. No filesystem I/O. |
| `_record_to_metadata()` + `_metadata_to_record_fields()` | Both must include `pipecat_version_pin`. Missing from either = silent data loss. |
| `DeprecationMap` on `HybridRetriever` | Loaded from disk JSON at startup. Rebuilt on each `refresh`. Accessed by `check_deprecation` tool via `retriever.deprecation_map`. |
| `check_deprecation` MCP tool | Dispatched through normal `handler(args, retriever)` path — no special-case needed. |
| `pipecat_version` tool parameter | Optional param (`default=None`) on search input models. Retriever protocol unaffected — new fields absorbed by Pydantic models. |

## Open Questions

1. **CHANGELOG parsing reliability** — prose sections are fragile. Seed
   manually for known deprecations, use CHANGELOG parsing as supplement.
2. **Should `check_deprecation` also check class/method names?** — Phase 1b
   covers module paths only. Expanding to class/method requires Phase 3 AST
   analysis.
3. **TS SDK version tracking** — `@pipecat-ai/client-js` uses `^1.7.0`.
   Store as separate `ts_version_pin` field or unified `pipecat_version_pin`?
   Recommend separate — different versioning schemes.

## Review Focus

- **Version extraction reliability** — PEP 508 extras, monorepo walk-upward
  (search for pipecat-ai, not just nearest pyproject), framework git tag
- **Deprecation map parsing** — bracket-expansion, fuzzy matching, CHANGELOG
  best-effort supplement
- **`check_deprecation` tool dispatch** — via `retriever.deprecation_map`,
  no special-case in `call_tool()`
- **Scoring balance** — -0.05 penalty, combined cap -0.10 with staleness
- **Backward compatibility** — new optional fields with `default=None`,
  Retriever protocol unchanged
- **`vector.py` round-trip** — both `_record_to_metadata()` AND
  `_metadata_to_record_fields()` must be updated

## Acceptance Criteria

- [x] `search_examples` results include `pipecat_version_pin` when available
- [x] `check_deprecation("pipecat.services.grok.llm")` returns deprecation
      info with replacement path
- [x] `check_deprecation("DailyTransport")` returns `{deprecated: false}`
- [x] Bracket-expansion: `check_deprecation("pipecat.services.cartesia.stt")`
      returns deprecation info
- [x] MCP server instructions mention `check_deprecation` for import checking
- [x] Existing 29 smoke tests still pass (no regressions)
- [x] `refresh --force` populates version pins on all chunks
- [x] Version info defaults to `None` gracefully for chunks without it
- [x] `packaging` declared as explicit dependency in pyproject.toml
