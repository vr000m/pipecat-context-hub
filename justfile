# Pipecat Context Hub — task runner
# https://github.com/casey/just

set dotenv-load

# List available recipes
default:
    @just --list

# ── Dev ──────────────────────────────────────────────

# Run the full test suite
test *args:
    uv run pytest tests/ -v {{args}}

# Run the live retrieval-quality benchmark against the local default index
benchmark-quality:
    PIPECAT_HUB_ENABLE_QUALITY_BENCHMARK=1 uv run pytest tests/benchmarks/test_retrieval_quality.py -m benchmark -v -s

# Lint with ruff
lint:
    uv run ruff check src/ tests/

# Format check with ruff
fmt-check:
    uv run ruff format --check src/ tests/

# Auto-format with ruff
fmt:
    uv run ruff format src/ tests/

# Type check with mypy
typecheck:
    uv run mypy src/ tests/

# Run lint + format check + type check
check: lint fmt-check typecheck

# Dependency vulnerability audit
audit-deps:
    uv run pip-audit --local --progress-spinner off --ignore-vuln CVE-2026-4539

# Static security scan for Python code
audit-security:
    uv run bandit -r src

# Generate a CycloneDX SBOM from the current locked environment
sbom out="artifacts/security/sbom.json":
    mkdir -p $(dirname {{out}})
    uv run cyclonedx-py environment --output-reproducible --of JSON -o {{out}}

# Run the local security gate
audit: audit-deps audit-security

# ── Server ───────────────────────────────────────────

# Start the MCP server
serve:
    uv run pipecat-context-hub serve

# Refresh the index (pass --force for full re-ingest)
refresh *args:
    uv run pipecat-context-hub refresh {{args}}

# ── Dashboard ────────────────────────────────────────

# Refresh index + rebuild all dashboard data
dashboard-refresh *args: (refresh args)
    @echo ""
    @echo "=== Extracting embeddings + UMAP 3D projection ==="
    uv run python dashboard/scripts/extract_embeddings.py
    @echo ""
    @echo "=== Computing K-means clusters ==="
    uv run python dashboard/scripts/compute_clusters.py
    @echo ""
    @echo "=== Extracting dashboard stats ==="
    uv run python dashboard/scripts/extract_dashboard.py
    @echo ""
    @echo "Done. Run 'just dashboard-serve' to view."

# Rebuild dashboard data without refreshing the index
dashboard-build:
    uv run python dashboard/scripts/extract_embeddings.py
    uv run python dashboard/scripts/compute_clusters.py
    uv run python dashboard/scripts/extract_dashboard.py

# Serve the dashboard on localhost:8765
dashboard-serve port="8765":
    python3 -m http.server {{port}} -d dashboard/public/
