"""CLI entry point for the Pipecat Context Hub.

Provides ``serve`` (default) and ``refresh`` commands.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from pipecat_context_hub.shared.config import HubConfig

# Shared sentinel used by refresh bookkeeping for missing/unknown cells
# (SHA, existing count, updated count). Centralised so the summary
# renderer and the producers cannot drift.
_MISSING_SENTINEL = "\u2014"

# Exit code for `serve` when the index cannot be used (unopenable or empty).
# Documented in CHANGELOG 0.0.17 "Changed" section.
_EXIT_INDEX_UNREADY = 2


def _redact_home(path: Path | str) -> str:
    """Replace the user's home-directory prefix with ``~`` for logs.

    Startup telemetry is included in the "share this with maintainers"
    guidance, so stripping usernames out of absolute paths keeps routine
    bug reports from leaking local filesystem layout.
    """
    try:
        s = str(path)
        home = str(Path.home())
        if home and (s == home or s.startswith(home + os.sep)):
            return "~" + s[len(home):]
        return s
    except Exception:
        return str(path)


def _load_dotenv() -> None:
    """Load ``.env`` file from the current directory if it exists.

    Only sets variables that are not already in the environment so that
    explicit env vars always take precedence.  Supports quoted values
    and inline comments::

        KEY="value"          # ok
        KEY='value'          # ok
        KEY=value            # ok
        KEY="value" # note   # inline comment stripped
        KEY=value # note     # inline comment stripped
    """
    env_path = Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Quoted value: extract content between matching quotes.
        if value and value[0] in ('"', "'"):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            # Unquoted: strip inline comments (# preceded by whitespace).
            idx = value.find(" #")
            if idx != -1:
                value = value[:idx].rstrip()
        if key not in os.environ:
            os.environ[key] = value


def _configure_logging(level: str) -> None:
    """Set up basic logging to stderr (stdout is used by MCP stdio transport)."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _delete_local_index_storage(data_dir: Path) -> None:
    """Delete the persisted local index directory for a clean rebuild."""
    shutil.rmtree(data_dir, ignore_errors=True)


@click.group(invoke_without_command=True)
@click.option("--log-level", default="INFO", help="Logging level.")
@click.pass_context
def main(ctx: click.Context, log_level: str) -> None:
    """Pipecat Context Hub — local-first MCP server."""
    _load_dotenv()
    _configure_logging(log_level)
    ctx.ensure_object(dict)
    config = HubConfig()
    ctx.obj["config"] = config.model_copy(
        update={"server": config.server.model_copy(update={"log_level": log_level})}
    )
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve)


@main.command()
@click.pass_context
def serve(ctx: click.Context) -> None:
    """Start the MCP server (stdio transport)."""
    # Capture PPID at the very top — before IndexStore/embedding/reranker
    # construction, which can take several seconds. If the client dies
    # during that startup window, os.getppid() has already flipped to 1
    # by the time run_stdio() snapshots it, and the watchdog would lock
    # in the already-reparented PID and never fire.
    _original_ppid = os.getppid()

    from pipecat_context_hub.server.main import create_server
    from pipecat_context_hub.server.transport import serve_stdio
    from pipecat_context_hub.shared.tracking import IdleTracker
    from pipecat_context_hub.services.embedding import EmbeddingService
    from pipecat_context_hub.services.index.store import IndexStore
    from pipecat_context_hub.services.retrieval.cross_encoder import CrossEncoderReranker
    from pipecat_context_hub.services.retrieval.hybrid import HybridRetriever

    config: HubConfig = ctx.obj["config"]
    logger = logging.getLogger(__name__)
    logger.info("Starting server with transport=%s", config.server.transport)

    index_store: IndexStore | None = None
    try:
        index_store = IndexStore(config.storage)
        stats = index_store.get_index_stats()
    except Exception as exc:
        # IndexStore.__init__ opens two backends (Chroma + SQLite) without
        # rolling back on partial failure; close() whatever did come up.
        if index_store is not None:
            try:
                index_store.close()
            except Exception:
                logger.exception("Failed to close partially-opened index store")
        logger.error(
            "Failed to open index at %s: %s. "
            "Run 'uv run pipecat-context-hub refresh --force --reset-index' to rebuild.",
            config.storage.data_dir,
            exc,
        )
        raise SystemExit(_EXIT_INDEX_UNREADY) from exc

    if stats.get("total", 0) == 0:
        logger.error(
            "Index at %s is empty (0 records). "
            "MCP clients would hang waiting for results. "
            "Run 'uv run pipecat-context-hub refresh' before 'serve'.",
            config.storage.data_dir,
        )
        index_store.close()
        raise SystemExit(_EXIT_INDEX_UNREADY)

    # Startup banner: one line so operators can confirm version, data dir,
    # and content-type shape from an MCP JSONL trace. Helps diagnose
    # "upgraded but still running the old exe" and "docs indexed but code
    # didn't" without extra tooling.
    #
    # Logs the raw counts_by_type mapping (whose keys — doc, code, source —
    # come straight from FTS) so a future content_type rename or new type
    # cannot silently zero out this signal. `data_dir` is redacted to ~/…
    # because server instructions now encourage clients to share startup
    # log lines in bug reports.
    from pipecat_context_hub.server.main import _SERVER_VERSION

    counts_by_type = stats.get("counts_by_type", {}) or {}
    counts_repr = ",".join(f"{k}={v}" for k, v in sorted(counts_by_type.items()))
    logger.info(
        "pipecat-context-hub v%s starting: data_dir=%s total=%d counts_by_type={%s}",
        _SERVER_VERSION,
        _redact_home(config.storage.data_dir),
        stats.get("total", 0),
        counts_repr or "(empty)",
    )

    # From here on, any failure must close the index_store — otherwise a
    # crash between open and serve_stdio() leaks Chroma/SQLite handles and
    # hinders the `refresh --reset-index` recovery path.
    try:
        embedding_svc = EmbeddingService(config.embedding)

        # Optional cross-encoder reranker (env var or config)
        from pipecat_context_hub.shared.config import _RERANKER_MODEL_ENV
        from pipecat_context_hub.shared.types import (
            RerankerDisabledReason,
            RerankerStatus,
        )

        cross_encoder: CrossEncoderReranker | None = None
        active_model = config.reranker.effective_model  # post-validation, in allowlist
        # Capture the operator's raw request so get_hub_status can surface
        # misconfigurations (e.g. typo in env var) even when silently falling
        # back to the default. Env wins over field when set.
        env_request = os.environ.get(_RERANKER_MODEL_ENV, "").strip()
        requested_model = env_request or config.reranker.cross_encoder_model
        startup_disabled_reason: RerankerDisabledReason | None = None
        if not config.reranker.effective_enabled:
            startup_disabled_reason = "config_disabled"
        else:
            # Check cache at startup — disable if model not downloaded
            if CrossEncoderReranker.is_model_cached(active_model):
                cross_encoder = CrossEncoderReranker(
                    model_name=active_model,
                    top_n=config.reranker.top_n,
                    enabled=True,
                )
                logger.info("Cross-encoder reranker enabled: %s", active_model)
            else:
                startup_disabled_reason = "not_cached"

        # Single telemetry line when the reranker is off at boot. Operators
        # grep this from MCP traces to diagnose degraded startups without
        # calling get_hub_status.
        if startup_disabled_reason is not None:
            hint = ""
            if startup_disabled_reason == "config_disabled":
                hint = (
                    "PIPECAT_HUB_RERANKER_ENABLED=0 (or config). "
                    "Unset the env var (or set it to 1) to re-enable."
                )
            elif startup_disabled_reason == "not_cached":
                probed = _redact_home(CrossEncoderReranker.resolve_hf_cache_dir())
                hint = (
                    f"model not downloaded (checked HF cache: {probed}). "
                    "Run 'pipecat-context-hub refresh' to pre-download, or set "
                    "PIPECAT_HUB_RERANKER_MODEL to a smaller cached model "
                    "(e.g. cross-encoder/ms-marco-TinyBERT-L-2-v2). "
                    "If this path is unexpected, check HF_HOME / HUGGINGFACE_HUB_CACHE."
                )
            logger.warning(
                "Reranker disabled at startup: reason=%s configured_model=%s — %s",
                startup_disabled_reason,
                requested_model or "(default)",
                hint,
            )

        def _reranker_status() -> RerankerStatus:
            """Compute live reranker status at get_hub_status query time."""
            if cross_encoder is None:
                return RerankerStatus(
                    enabled=False,
                    configured_model=requested_model,
                    disabled_reason=startup_disabled_reason,
                )
            if cross_encoder.enabled:
                return RerankerStatus(
                    enabled=True,
                    model=active_model,
                    configured_model=requested_model,
                )
            # Reranker was constructed but its .enabled flipped to False —
            # first model-load attempt failed at runtime.
            return RerankerStatus(
                enabled=False,
                configured_model=requested_model,
                disabled_reason="load_failed",
            )

        retriever = HybridRetriever(index_store, embedding_svc, cross_encoder=cross_encoder)

        # Load deprecation map from disk if available
        from pipecat_context_hub.services.ingest.deprecation_map import DeprecationMap

        dep_map_path = config.storage.data_dir / "deprecation_map.json"
        retriever.deprecation_map = DeprecationMap.load(dep_map_path)
        if retriever.deprecation_map.entries:
            logger.info(
                "Loaded deprecation map: %d entries", len(retriever.deprecation_map.entries)
            )

        idle_tracker = IdleTracker()
        server = create_server(
            retriever,
            index_store,
            reranker_status_provider=_reranker_status,
            idle_tracker=idle_tracker,
        )
        def _close_index_store_for_hard_exit() -> None:
            """Release index handles on the watchdog hard-exit path.

            `run_stdio` calls this just before `os._exit(0)` when a
            graceful unwind can't complete (Linux stdin-reader thread
            stuck in a blocked syscall). Since `os._exit` skips the
            outer `finally`, closing the store here keeps Chroma's
            SQLite WAL / FTS handles from being leaked on abrupt exit.
            """
            try:
                index_store.close()
            except Exception:
                logger.exception("Failed to close index store on hard exit")

        serve_stdio(
            server,
            original_ppid=_original_ppid,
            idle_tracker=idle_tracker,
            parent_watch_interval_secs=config.server.effective_parent_watch_interval_secs,
            idle_timeout_secs=config.server.effective_idle_timeout_secs,
            on_hard_exit=_close_index_store_for_hard_exit,
            exit_on_watchdog_shutdown=True,
        )
    finally:
        index_store.close()


@main.command()
@click.option("--force", is_flag=True, help="Force full refresh, ignoring cached state.")
@click.option(
    "--reset-index",
    is_flag=True,
    help="Delete local index state before rebuilding. Use this when the persisted Chroma index is unhealthy.",
)
@click.option(
    "--framework-version",
    default=None,
    help="Pin the framework repo (pipecat-ai/pipecat) to a specific git tag "
    "(e.g. 'v0.0.96'). Source chunks will come from that version instead of HEAD. "
    "Can also be set via PIPECAT_HUB_FRAMEWORK_VERSION env var.",
)
@click.pass_context
def refresh(ctx: click.Context, force: bool, reset_index: bool, framework_version: str | None) -> None:
    """Rebuild the index, skipping unchanged sources when possible."""
    from pipecat_context_hub.services.embedding import (
        EmbeddingIndexWriter,
        EmbeddingService,
    )
    from pipecat_context_hub.services.index.store import IndexStore
    from pipecat_context_hub.services.ingest.docs_crawler import DocsCrawler
    from pipecat_context_hub.services.ingest.github_ingest import (
        _FRAMEWORK_REPO,
        GitHubRepoIngester,
        repo_ref_is_tainted,
    )
    from pipecat_context_hub.services.ingest.source_ingest import SourceIngester

    logger = logging.getLogger(__name__)
    config: HubConfig = ctx.obj["config"]

    # Propagate --framework-version CLI flag into config (CLI > env var).
    if framework_version is not None:
        config = config.model_copy(update={"framework_version": framework_version})

    fw_version = config.effective_framework_version
    logger.info(
        "Starting index refresh (force=%s reset_index=%s framework_version=%s)",
        force,
        reset_index,
        fw_version,
    )
    start = time.monotonic()

    if reset_index:
        logger.warning("Deleting local index storage before refresh")
        _delete_local_index_storage(config.storage.data_dir)
        force = True

    # Build the ingestion pipeline
    index_store = IndexStore(config.storage)
    embedding_svc = EmbeddingService(config.embedding)
    writer = EmbeddingIndexWriter(index_store, embedding_svc)

    # Pre-download cross-encoder model if enabled (env var or config)
    if config.reranker.effective_enabled:
        from pipecat_context_hub.services.retrieval.cross_encoder import CrossEncoderReranker

        ce = CrossEncoderReranker(
            model_name=config.reranker.effective_model,
            enabled=True,
        )
        ce.ensure_model()

    total_upserted = 0
    all_errors: list[str] = []

    # Per-source tracking for the summary table.
    # Each entry: {status, sha, existing, updated}
    source_status: dict[str, dict[str, str | int]] = {}

    # Built inside _run_refresh; read later by the summary pass. Created
    # once per refresh invocation, so no cross-run state leakage.
    github = GitHubRepoIngester(config, writer)

    async def _run_refresh() -> None:
        nonlocal total_upserted, all_errors

        # Snapshot per-repo chunk counts before any changes.
        pre_counts = index_store.get_counts_by_repo()

        # ----- 1. Docs -----
        crawler = DocsCrawler(writer, config.sources, config.chunking)
        docs_key = "docs.pipecat.ai"
        try:
            raw_text = await crawler.fetch_llms_txt()
        except Exception as exc:
            all_errors.append(f"Failed to fetch llms-full.txt: {exc}")
            raw_text = None
            source_status[docs_key] = {
                "status": "error",
                "sha": _MISSING_SENTINEL,
                "existing": pre_counts.get(docs_key, 0),
                "updated": _MISSING_SENTINEL,
            }

        if raw_text is not None:
            content_hash = hashlib.sha256(raw_text.encode()).hexdigest()
            stored_hash = index_store.get_metadata("docs:content_hash")
            if not force and stored_hash == content_hash:
                logger.info("Docs unchanged (hash=%s…), skipping", content_hash[:8])
                source_status[docs_key] = {
                    "status": "skipped",
                    "sha": _MISSING_SENTINEL,
                    "existing": pre_counts.get(docs_key, 0),
                    "updated": _MISSING_SENTINEL,
                }
            else:
                await index_store.delete_by_content_type("doc")
                docs_result = await crawler.ingest(prefetched_text=raw_text)
                total_upserted += docs_result.records_upserted
                all_errors.extend(docs_result.errors)
                logger.info(
                    "Docs crawl: upserted=%d errors=%d",
                    docs_result.records_upserted,
                    len(docs_result.errors),
                )
                if not docs_result.errors:
                    index_store.set_metadata("docs:content_hash", content_hash)
                source_status[docs_key] = {
                    "status": "error" if docs_result.errors else "updated",
                    "sha": _MISSING_SENTINEL,
                    "existing": pre_counts.get(docs_key, 0),
                    "updated": docs_result.records_upserted,
                }
        await crawler.close()

        # ----- 2. Repos (code + source) -----
        changed_repos: list[str] = []
        repo_shas: dict[str, str] = {}
        prefetched: dict[str, tuple[Path, str]] = {}
        frozen_sha_repos: set[str] = set()

        # Clean up repos removed from configuration (P2: stale data from
        # repos no longer in effective_repos would persist indefinitely).
        configured = set(config.sources.effective_repos)
        tainted_repos = set(config.sources.tainted_repos)
        all_meta = index_store.get_all_metadata()
        for meta_key in all_meta:
            if meta_key.startswith("repo:") and meta_key.endswith(":commit_sha"):
                slug = meta_key[len("repo:"):-len(":commit_sha")]
                if slug not in configured:
                    if slug in tainted_repos:
                        logger.warning("Repo %s is tainted by local policy, cleaning up", slug)
                    else:
                        logger.info("Repo %s no longer configured, cleaning up", slug)
                    await index_store.delete_by_repo(slug)
                    index_store.delete_metadata(meta_key)

        framework_slug = _FRAMEWORK_REPO
        for repo_slug in config.sources.effective_repos:
            stored_sha_key = f"repo:{repo_slug}:commit_sha"
            # Pin the framework repo to a specific tag when configured.
            repo_tag = fw_version if repo_slug == framework_slug and fw_version else None
            try:
                repo_path, commit_sha = await asyncio.to_thread(
                    github.clone_or_fetch, repo_slug, False, tag=repo_tag
                )
                repo_shas[repo_slug] = commit_sha
                prefetched[repo_slug] = (repo_path, commit_sha)
            except Exception as exc:
                all_errors.append(f"Failed to clone/fetch {repo_slug}: {exc}")
                source_status[repo_slug] = {
                    "status": "error",
                    "sha": _MISSING_SENTINEL,
                    "existing": pre_counts.get(repo_slug, 0),
                    "updated": _MISSING_SENTINEL,
                }
                continue

            stored_sha = index_store.get_metadata(stored_sha_key)
            tainted_refs = set(config.sources.tainted_refs_by_repo.get(repo_slug, []))
            if tainted_refs and repo_ref_is_tainted(repo_path, commit_sha, tainted_refs):
                logger.warning(
                    "Repo %s resolved to tainted ref (sha=%s), skipping refresh",
                    repo_slug,
                    commit_sha[:8],
                )
                if stored_sha and repo_ref_is_tainted(repo_path, stored_sha, tainted_refs):
                    logger.warning(
                        "Indexed ref for %s is also tainted; removing local records",
                        repo_slug,
                    )
                    await index_store.delete_by_repo(repo_slug)
                    index_store.delete_metadata(stored_sha_key)
                    source_status[repo_slug] = {
                        "status": "tainted",
                        "sha": commit_sha[:8],
                        "existing": pre_counts.get(repo_slug, 0),
                        "updated": 0,
                    }
                else:
                    source_status[repo_slug] = {
                        "status": "tainted",
                        "sha": commit_sha[:8],
                        "existing": pre_counts.get(repo_slug, 0),
                        "updated": _MISSING_SENTINEL,
                    }
                # Preserve the last known-good SHA (or lack of one) until this
                # repo is ingested successfully at a non-tainted ref.
                frozen_sha_repos.add(repo_slug)
                continue

            if (
                not force
                and stored_sha == commit_sha
                and repo_slug not in github.recovered_repos
            ):
                logger.info(
                    "Repo %s unchanged (sha=%s…), skipping",
                    repo_slug,
                    commit_sha[:8],
                )
                source_status[repo_slug] = {
                    "status": "skipped",
                    "sha": commit_sha[:8],
                    "existing": pre_counts.get(repo_slug, 0),
                    "updated": _MISSING_SENTINEL,
                }
            else:
                if repo_slug in github.recovered_repos and stored_sha == commit_sha:
                    logger.warning(
                        "Repo %s SHA unchanged but local clone was recovered "
                        "from corrupt state — forcing re-ingest",
                        repo_slug,
                    )
                changed_repos.append(repo_slug)

        # Delete and re-ingest each changed repo atomically to minimise
        # the window where a repo's index is empty (crash-safety).
        ingested_repos: set[str] = set()
        for repo_slug in changed_repos:
            repo_path, commit_sha = prefetched[repo_slug]
            try:
                await asyncio.to_thread(github.checkout_commit, repo_path, commit_sha)
            except Exception as exc:
                msg = f"Failed to checkout fetched ref for {repo_slug}: {exc}"
                all_errors.append(msg)
                logger.error(msg)
                source_status[repo_slug] = {
                    "status": "error",
                    "sha": commit_sha[:8],
                    "existing": pre_counts.get(repo_slug, 0),
                    "updated": _MISSING_SENTINEL,
                }
                continue

            await index_store.delete_by_repo(repo_slug)
            logger.info("Deleted stale records for %s", repo_slug)

            repo_has_errors = False
            repo_upserted = 0

            # Code ingest (per-repo for error tracking)
            code_result = await github.ingest(
                repos=[repo_slug], prefetched=prefetched,
            )
            total_upserted += code_result.records_upserted
            repo_upserted += code_result.records_upserted
            all_errors.extend(code_result.errors)
            if code_result.errors:
                repo_has_errors = True
            logger.info(
                "GitHub ingest (%s): upserted=%d errors=%d",
                repo_slug,
                code_result.records_upserted,
                len(code_result.errors),
            )

            # Source ingest
            source_ingester = SourceIngester(config, writer, repo_slug)
            source_result = await source_ingester.ingest()
            total_upserted += source_result.records_upserted
            repo_upserted += source_result.records_upserted
            all_errors.extend(source_result.errors)
            if source_result.errors:
                repo_has_errors = True
            if source_result.records_upserted > 0:
                logger.info(
                    "Source ingest (%s): upserted=%d errors=%d",
                    repo_slug,
                    source_result.records_upserted,
                    len(source_result.errors),
                )

            source_status[repo_slug] = {
                "status": "error" if repo_has_errors else "updated",
                "sha": repo_shas.get(repo_slug, _MISSING_SENTINEL)[:8],
                "existing": pre_counts.get(repo_slug, 0),
                "updated": repo_upserted,
            }

            if not repo_has_errors:
                ingested_repos.add(repo_slug)

        # Store SHAs: unchanged repos (handles first-run) + successfully ingested repos.
        # For failed repos: delete the cached SHA so the next non-force refresh
        # retries them (P1: --force deletes records before ingest, so a failure
        # leaves the repo empty; keeping the old SHA would skip it next time).
        for repo_slug, sha in repo_shas.items():
            if repo_slug in frozen_sha_repos:
                continue
            if repo_slug not in changed_repos or repo_slug in ingested_repos:
                index_store.set_metadata(f"repo:{repo_slug}:commit_sha", sha)
            else:
                index_store.delete_metadata(f"repo:{repo_slug}:commit_sha")

        # ----- 3. Deprecation map -----
        # Release-notes parsing (primary source) is always HEAD-independent.
        # Source and CHANGELOG scanning use whatever checkout is current —
        # when --framework-version is set, these reflect the pinned tag.
        # This is acceptable: release notes carry the bulk of deprecation data.
        from pipecat_context_hub.services.ingest.deprecation_map import (
            build_deprecation_map_from_changelog,
            build_deprecation_map_from_releases,
            build_deprecation_map_from_source,
        )

        dep_map_path = config.storage.data_dir / "deprecation_map.json"

        if framework_slug in prefetched:
            fw_path, fw_sha = prefetched[framework_slug]
            dep_map = build_deprecation_map_from_source(fw_path, commit_sha=fw_sha)
            dep_map = build_deprecation_map_from_releases(
                framework_slug, dep_map
            )
            changelog = fw_path / "CHANGELOG.md"
            dep_map = build_deprecation_map_from_changelog(
                changelog, dep_map, repo_root=fw_path
            )
            dep_map.save(dep_map_path)
        else:
            # Framework repo not cloned — still try release notes via gh
            from pipecat_context_hub.services.ingest.deprecation_map import (
                DeprecationMap,
            )
            existing = DeprecationMap.load(dep_map_path) if dep_map_path.is_file() else DeprecationMap()
            dep_map = build_deprecation_map_from_releases(
                framework_slug, existing
            )
            if dep_map.entries:
                dep_map.save(dep_map_path)
                logger.info(
                    "Built deprecation map from release notes only "
                    "(%d entries — framework repo not cloned)",
                    len(dep_map.entries),
                )
            else:
                logger.debug(
                    "Framework repo %s not in effective_repos and no "
                    "release notes available — preserving existing map",
                    framework_slug,
                )

    try:
        asyncio.run(_run_refresh())

        duration = round(time.monotonic() - start, 1)
        logger.info(
            "Refresh complete: upserted=%d errors=%d duration=%.1fs",
            total_upserted,
            len(all_errors),
            duration,
        )
        if all_errors:
            for err in all_errors:
                logger.warning("  %s", err)

        # Persist refresh metadata for get_hub_status tool.
        now = datetime.now(timezone.utc).isoformat()
        index_store.set_metadata("last_refresh_duration_seconds", str(duration))
        index_store.set_metadata("last_refresh_records_upserted", str(total_upserted))
        index_store.set_metadata("last_refresh_error_count", str(len(all_errors)))

        stats = index_store.get_index_stats()
        index_store.set_metadata("content_type_counts", json.dumps(stats["counts_by_type"]))

        # Persist pinned framework version (or clear it) for get_hub_status.
        if fw_version:
            index_store.set_metadata("framework_version", fw_version)
        else:
            index_store.delete_metadata("framework_version")

        index_store.set_metadata("last_refresh_at", now)
        if all_errors:
            index_store.set_metadata("last_refresh_errored_at", now)

        # ----- Summary table -----
        _print_refresh_summary(
            source_status,
            total_upserted,
            len(all_errors),
            duration,
            recovered_repos=sorted(github.recovered_repos),
        )
    finally:
        index_store.close()


def _stdout_can_encode(text: str) -> bool:
    """Return True if ``sys.stdout`` can encode ``text`` without errors.

    Re-reads ``sys.stdout`` on every call so tests (and callers) can swap
    the stream. A missing ``encoding`` attribute is treated as ``ascii``.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _encode_safe(value: str, fallback: str) -> str:
    """Return ``value`` if stdout can encode every character, else ``fallback``."""
    return value if _stdout_can_encode(value) else fallback


def _safe_hr(width: int) -> str:
    """Return a horizontal-rule string of ``width`` characters.

    Uses U+2500 (box-drawing light horizontal) when ``sys.stdout`` can encode
    it; falls back to ASCII ``-`` on non-UTF-8 consoles (cp1252, cp1254,
    cp437, etc.) so the refresh summary never crashes after a successful
    index.
    """
    if not _stdout_can_encode("\u2500"):
        return "-" * width
    return "\u2500" * width


def _safe_placeholder() -> str:
    """Return a missing-value placeholder that the current stdout can encode.

    U+2014 em dash on UTF-8 terminals; ASCII ``-`` when stdout cannot encode
    it (cp437 notably rejects U+2014). Used for empty SHA / count cells in
    the refresh summary.
    """
    if _stdout_can_encode("\u2014"):
        return "\u2014"
    return "-"


def _print_refresh_summary(
    source_status: dict[str, dict[str, str | int]],
    total_upserted: int,
    error_count: int,
    duration: float,
    *,
    recovered_repos: list[str] | None = None,
) -> None:
    """Print a summary table after refresh."""
    if not source_status:
        click.echo(f"Refresh complete: {total_upserted} records upserted in {duration}s.")
        return

    # Compute column widths
    name_width = max(len(name) for name in source_status)
    name_width = max(name_width, len("Repository"))

    hr = (
        f"{_safe_hr(name_width)}  {_safe_hr(8)}  {_safe_hr(10)}  "
        f"{_safe_hr(8)}  {_safe_hr(8)}"
    )
    placeholder = _safe_placeholder()

    # Header
    click.echo()
    click.echo(
        f"{'Repository':<{name_width}}  {'Status':<8}  {'SHA':<10}  {'Existing':>8}  {'Updated':>8}"
    )
    click.echo(hr)

    # Rows — updated/error first, then skipped
    total_existing = 0
    total_updated = 0
    for name in sorted(source_status, key=lambda n: (source_status[n]["status"] == "skipped", n)):
        entry = source_status[name]
        status = str(entry["status"])
        # Normalise any non-encodable cell (typically the missing-value
        # sentinel) to the placeholder so the summary never crashes on
        # terminals that cannot encode it.
        sha = _encode_safe(str(entry["sha"]), placeholder)
        existing = entry["existing"]
        updated = entry["updated"]

        existing_int = int(existing) if isinstance(existing, int) else 0
        total_existing += existing_int

        if isinstance(updated, int):
            total_updated += updated
            updated_str = f"{updated:,}"
        elif status == "skipped":
            # Skipped repos carry forward their existing count —
            # their chunks are still in the index unchanged.
            total_updated += existing_int
            updated_str = placeholder
        else:
            # Error repos: don't carry forward (chunks may have been deleted).
            updated_str = placeholder

        existing_str = f"{existing_int:,}" if existing_int else placeholder

        click.echo(
            f"{name:<{name_width}}  {status:<8}  {sha:<10}  {existing_str:>8}  {updated_str:>8}"
        )

    # Footer
    click.echo(hr)
    click.echo(
        f"{'Total':<{name_width}}  {'':<8}  {'':<10}  {total_existing:>8,}  {total_updated:>8,}"
    )
    click.echo()
    if recovered_repos:
        click.echo(
            f"Recovered {len(recovered_repos)} corrupt clone(s): "
            f"{', '.join(recovered_repos)}"
        )
    click.echo(
        f"Refresh complete: {total_upserted:,} upserted, "
        f"{error_count} errors, {duration}s."
    )


# Enables ``python -m pipecat_context_hub.cli`` invocation, which the
# integration test uses to launch `serve` as a direct child of the test
# wrapper (avoiding the `uv run` intermediate that prevents the PPID
# watchdog from firing).
if __name__ == "__main__":
    main()
