# MCP Server Audit & Hardening

## Header
- **Status:** In Progress
- **Type:** chore
- **Assignee:** vr000m
- **Priority:** High
- **Working Branch:** chore/mcp-server-audit
- **Created:** 2026-03-25
- **Target Completion:** 2026-03-31
- **Objective:** Perform a focused code, architecture, and release-process audit for the MCP server and its refresh/runtime pipeline, then add the missing hardening and review gates for supply chain safety, upstream taint handling, resource lifecycle, and maintainability.

## Context

Pipecat Context Hub runs on developers' machines and has multiple trust boundaries that deserve stricter review than a typical local-only utility. The MCP server path fetches remote documentation, clones and resets GitHub repositories, downloads local ML models, persists a local Chroma/SQLite index, and exposes the result over stdio.

That combination means the review needs to cover more than code style or isolated bugs. The audit should verify that remote inputs remain data-only, dependency installation is reproducible, destructive local operations stay scoped to the intended data directory, long-lived resources shut down cleanly, and the project has repeatable release gates instead of relying on ad hoc manual review.

This plan is intentionally scoped to the MCP server and refresh/runtime surfaces. Static dashboard assets and client config templates are out of scope unless they directly affect the server runtime, ingestion safety, or release documentation for the audited path.

## Initial Findings (Historical Baseline)

- **High:** Before this branch, the install guidance in [docs/README.md](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/docs/README.md) used `uv pip install -e ".[dev]"`, while the repo also carried `uv.lock`. That bypassed lockfile-based reproducibility and increased dependency drift risk on developer machines.
- **High:** Before this branch, the repository did not include committed CI/security automation under `.github/workflows/`, and there was no repo-local evidence of automated vulnerability scanning, OSV scanning, SBOM generation, or dependency update policy enforcement.
- **High:** Before this branch, existing tests covered correctness and retrieval benchmarks, but there was no dedicated soak or leak harness for repeated `refresh`/`serve` cycles, concurrent tool calls, or RSS/thread/file-descriptor growth over time.
- **High:** Before this branch, the refresh path tracked mutable upstream content directly: docs were fetched live, Git repos were reset to remote HEAD, and extra repos could be appended from environment configuration. There was no repo-local policy for marking an upstream repo, tag, release, or commit as tainted and skipping it.
- **Medium:** The highest-risk manual review targets are the remote ingestion and local persistence boundaries in [`cli.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/cli.py), [`server/main.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/server/main.py), [`server/transport.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/server/transport.py), [`docs_crawler.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/ingest/docs_crawler.py), [`github_ingest.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/ingest/github_ingest.py), [`source_ingest.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/ingest/source_ingest.py), [`embedding.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/embedding.py), [`cross_encoder.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/retrieval/cross_encoder.py), [`vector.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/index/vector.py), and [`store.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/index/store.py).
- **Medium:** The repo has useful point defenses already, but they are not yet backed by a documented threat model or release gate. That raises the chance of future regressions in path containment, model loading policy, or resource cleanup.

## Existing Hardening To Preserve

- Repo slug sanitization and resolved-path containment in [`github_ingest.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/ingest/github_ingest.py).
- Symlink and out-of-repo guards before file reads in [`source_ingest.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/ingest/source_ingest.py).
- Cross-encoder model allowlisting in [`cross_encoder.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/retrieval/cross_encoder.py).
- Async offloading of synchronous vector and keyword queries in [`store.py`](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/src/pipecat_context_hub/services/index/store.py).
- Recent Chroma reset and shutdown support in the index lifecycle.

## Requirements

- Produce a written threat model and architecture review for all remote-input and local-persistence trust boundaries.
- Add repo-local automated review gates for linting, typing, tests, dependency vulnerability scanning, static security scanning, and SBOM generation.
- Move install and update guidance to a lockfile-based workflow so developer setups are reproducible.
- Add runtime validation for resource cleanup, leak detection, and concurrent request behavior.
- Add a local upstream-taint policy so a repo, release, tag, or commit can be skipped if a security incident or compromised release is identified upstream.
- Review module boundaries and duplicated logic, but only refactor when duplication creates correctness, security, or maintenance risk.
- Record accepted risks explicitly in [AGENTS.md](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/AGENTS.md) so future reviews do not rediscover deliberate trade-offs.

## Implementation Checklist

- [x] Create `chore/mcp-server-audit` and keep implementation off `main`.
- [x] Write a threat model and architecture review covering docs fetch, GitHub ingestion, HuggingFace model handling, local index persistence, CLI entrypoints, and MCP server entrypoints.
- [x] Add automated audit commands and CI workflows for `ruff`, `mypy`, `pytest`, dependency audit, static security scan, and SBOM generation.
- [x] Normalize the local dependency workflow so lockfile-based setup is explicit and reproducible, then replace lockfile-bypassing install guidance with the supported `uv` commands.
- [x] Add upstream taint-handling policy and enforcement for compromised repos, releases, tags, or commits, including a documented local skip path.
- [x] Add a soak/leak test path for repeated `refresh`/`serve` flows and concurrent retrieval calls, with observable RSS/thread/file-descriptor reporting.
- [ ] Perform a manual code and architecture review of the high-risk modules listed in this plan and record findings and remediations.
- [ ] Run a duplication and complexity audit and only consolidate code where the duplication creates real maintenance or correctness risk.
- [ ] Re-run the full review gate, update docs, and run `/deep-review` before merge.

## Technical Specifications

- Likely files and directories to modify:
  - `.github/workflows/`
  - `pyproject.toml`
  - `uv.lock`
  - `justfile`
  - `docs/README.md`
  - `AGENTS.md`
  - `docs/dev_plans/README.md`
  - `src/pipecat_context_hub/shared/config.py`
  - `tests/`
  - optional new `scripts/` or `docs/security/` content
- High-risk review inventory:
  - `src/pipecat_context_hub/cli.py`
  - `src/pipecat_context_hub/server/main.py`
  - `src/pipecat_context_hub/server/transport.py`
  - `src/pipecat_context_hub/services/embedding.py`
  - `src/pipecat_context_hub/services/ingest/docs_crawler.py`
  - `src/pipecat_context_hub/services/ingest/github_ingest.py`
  - `src/pipecat_context_hub/services/ingest/source_ingest.py`
  - `src/pipecat_context_hub/services/index/store.py`
  - `src/pipecat_context_hub/services/index/vector.py`
  - `src/pipecat_context_hub/services/retrieval/cross_encoder.py`
- Architecture checks:
  - Remote content from docs, GitHub, and model registries must remain data-only and must not create an execution path.
  - All destructive filesystem operations must stay scoped to `storage.data_dir` and be explicit in the CLI surface.
  - Long-lived services must have tested lifecycle behavior for startup, concurrency, and shutdown.
  - Dependency install and update paths must rely on pinned inputs instead of version ranges alone.
  - Release notes or security advisories from upstream projects are signals, not enforcement. The enforcement path must be local configuration that can skip a tainted repo or specific upstream ref even when it still exists upstream.
  - The refresh path currently tracks mutable upstream state; the audit must decide whether to add denylisting only, optional ref pinning, or both.
  - Review output should separate true findings from accepted trade-offs already documented in [AGENTS.md](/Users/vr000m/Code/pipecat-ai/pipecat-code-mcp/AGENTS.md).

## Review Focus

- Treat docs content, cloned repositories, environment variables, and model artifacts as untrusted input.
- Verify that `refresh` and `serve` do not read, write, or delete outside the intended local storage boundary.
- Confirm that network clients, SQLite connections, Chroma state, worker threads, and model resources do not accumulate across repeated operations.
- Review supply-chain exposure at install time, refresh time, and release time rather than only runtime request handling.
- Check upstream release/security notes as an input to the review, but require a local mechanism to skip a known-bad repo or upstream ref.
- Prefer small local hardening changes over broad refactors unless the architecture itself is causing risk.

## Testing Notes

- Baseline quality gate:
  - `uv run pytest tests/ -v`
  - `uv run mypy src/ tests/`
  - `uv run ruff check src/ tests/`
- Proposed security and supply-chain gate:
  - `uv run pip-audit --local --progress-spinner off --ignore-vuln CVE-2026-4539`
  - `uv run bandit -r src`
  - `uv run cyclonedx-py environment --output-reproducible --of JSON -o artifacts/security/sbom.json`
- Upstream taint validation:
  - Add tests for denylisted repo slugs and denylisted refs so refresh refuses or skips them predictably.
  - Add tests for the selected policy on existing indexed data when an upstream source becomes tainted after a prior ingest.
- Runtime validation:
  - Add a dedicated soak/leak test target for repeated `refresh`/`serve` cycles and concurrent MCP tool calls.
  - Capture RSS, thread count, and file-descriptor growth during the soak run.
  - Use `memray` or `tracemalloc` for targeted profiling if the soak run shows growth.
- Some of the heavier profiling and security tooling may need to run outside the current read-only Codex sandbox.
- Completed in this slice:
  - `uv sync --extra dev --group dev`
  - `uv run pytest tests/unit/test_config.py tests/unit/test_github_ingest.py tests/unit/test_cli.py -q`
  - `uv run ruff check src/pipecat_context_hub/shared/config.py src/pipecat_context_hub/services/ingest/github_ingest.py src/pipecat_context_hub/cli.py tests/unit/test_config.py tests/unit/test_github_ingest.py tests/unit/test_cli.py`
  - `uv run mypy src/pipecat_context_hub/shared/config.py src/pipecat_context_hub/services/ingest/github_ingest.py src/pipecat_context_hub/cli.py tests/unit/test_config.py tests/unit/test_github_ingest.py tests/unit/test_cli.py`
  - `uv run pytest tests/ -q`
  - `uv run ruff check src/ tests/`
  - `uv run mypy src/ tests/`
  - `uv run bandit -r src`
  - `uv run pip-audit --local --progress-spinner off --ignore-vuln CVE-2026-4539`
  - `uv run cyclonedx-py environment --output-reproducible --of JSON -o /tmp/pipecat-audit/sbom.json`
  - `just --dry-run sbom /tmp/pipecat-audit-just/sbom.json`
  - `PIPECAT_HUB_ENABLE_STABILITY_BENCHMARK=1 uv run pytest tests/benchmarks/test_runtime_stability.py -q -s`
  - `pip-audit` initially surfaced `requests 2.32.5`, `pyjwt 2.11.0`, and `pygments 2.19.2`; the first two were remediated by pinning fixed versions, while `pygments` is recorded as an accepted risk because `pip-audit` does not currently report a fixed PyPI release.
  - The new runtime benchmark initially reproduced a concurrent `search_docs` crash under opt-in load; the fix serialized access to the shared sentence-transformers model, Chroma client, and SQLite connection.

## Issues & Solutions

- The current worktree is on `main`.
  Solution: create `chore/mcp-server-audit` before making non-doc implementation changes.
- The recommended security tools are not yet part of the repo's declared dev workflow.
  Solution: decide which tools belong in `pyproject.toml` versus CI-only setup, then pin and document the chosen path.
- Upstream compromise information may appear in release notes, issue trackers, or security advisories, but the current server refresh path ingests mutable upstream state directly.
  Solution: make release/security review an operator workflow input, then enforce the decision locally via skip or denylist configuration.
- Leak and soak validation are harder to prove in a constrained assistant sandbox than on a normal developer machine or CI runner.
  Solution: add the harness and commands in-repo, then capture local/CI artifacts as part of the review record.
- `pip-audit` initially failed on transitive runtime/dev dependencies.
  Solution: move the environment to fixed `requests` and `pyjwt` resolutions during the audit pass, then remove the unused direct pins once the lockfile continued to resolve to the fixed versions. The remaining `pygments` advisory stays documented as an explicit accepted risk until upstream publishes a fix.
- The first version of the runtime stability benchmark exposed a real concurrency crash during parallel retrieval.
  Solution: add per-process locks around the shared embedding model, Chroma client, and SQLite connection so async fan-out no longer drives unsafe concurrent native calls.
- The docs fetch path trusted `llms-full.txt` to stay reasonably sized.
  Solution: stream the response and reject payloads above a fixed safety cap so an unexpectedly large upstream response cannot grow process memory without bound during refresh.

## Acceptance Criteria

- [x] A written threat model and architecture review exist for the repo's trust boundaries.
- [x] The repo contains an automated review gate for quality, security, and supply-chain checks.
- [x] Install and update documentation use a reproducible, lockfile-based workflow.
- [x] Refresh can skip or denylist a tainted upstream repo or specific upstream ref by local policy.
- [x] The project has repeatable soak/leak validation for the long-lived server and refresh paths.
- [ ] Critical and high-severity findings are fixed or explicitly accepted and documented.
- [ ] README, AGENTS, and the active dev plan reflect the final review process and residual risks.
- [ ] `/deep-review` completes with no unresolved critical or high-severity findings.

## Final Results

- In progress.
- Completed slices:
  - local tainted-upstream denylisting for repos and refs, with pre-checkout enforcement
  - tainted-ref refresh now preserves last-known-good SHA state instead of writing blocked upstream SHAs into index metadata
  - tainted tag inspection now fails closed if the local checkout cannot be inspected safely
  - lockfile-based install workflow in docs
  - written MCP server threat model in `docs/security/threat-model.md`
  - repo-local CI workflow plus `just` audit/SBOM commands, with `uv` bootstrapped through Astral's official `setup-uv` action instead of `pip install`
  - opt-in runtime stability benchmark for repeated `refresh` / `serve` cycles and concurrent retrieval rounds
  - concurrency hardening for shared embedding, Chroma, and SQLite access after the benchmark reproduced a crash under load
  - bounded streaming fetch for `llms-full.txt` so remote docs ingestion now enforces a maximum payload size
  - repo-wide quality/security gate now passes with one documented `pip-audit` ignore for `pygments` (`CVE-2026-4539`) pending an upstream fixed release
- Remaining slices:
  - manual high-risk module review and duplication audit
  - final `/deep-review` before merge
