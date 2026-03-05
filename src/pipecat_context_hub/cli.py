"""CLI entry point for the Pipecat Context Hub.

Provides ``serve`` (default) and ``refresh`` commands.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from pipecat_context_hub.shared.config import HubConfig


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
    from pipecat_context_hub.server.main import create_server
    from pipecat_context_hub.server.transport import serve_stdio
    from pipecat_context_hub.services.embedding import EmbeddingService
    from pipecat_context_hub.services.index.store import IndexStore
    from pipecat_context_hub.services.retrieval.hybrid import HybridRetriever

    config: HubConfig = ctx.obj["config"]
    logger = logging.getLogger(__name__)
    logger.info("Starting server with transport=%s", config.server.transport)

    index_store = IndexStore(config.storage)
    embedding_svc = EmbeddingService(config.embedding)
    retriever = HybridRetriever(index_store, embedding_svc)

    server = create_server(retriever, index_store)
    serve_stdio(server)


@main.command()
@click.option("--force", is_flag=True, help="Force full refresh, ignoring cached state.")
@click.pass_context
def refresh(ctx: click.Context, force: bool) -> None:
    """Rebuild the index, skipping unchanged sources when possible."""
    from pipecat_context_hub.services.embedding import (
        EmbeddingIndexWriter,
        EmbeddingService,
    )
    from pipecat_context_hub.services.index.store import IndexStore
    from pipecat_context_hub.services.ingest.docs_crawler import DocsCrawler
    from pipecat_context_hub.services.ingest.github_ingest import GitHubRepoIngester
    from pipecat_context_hub.services.ingest.source_ingest import SourceIngester

    logger = logging.getLogger(__name__)
    config: HubConfig = ctx.obj["config"]

    logger.info("Starting index refresh (force=%s)", force)
    start = time.monotonic()

    # Build the ingestion pipeline
    index_store = IndexStore(config.storage)
    embedding_svc = EmbeddingService(config.embedding)
    writer = EmbeddingIndexWriter(index_store, embedding_svc)

    total_upserted = 0
    all_errors: list[str] = []

    async def _run_refresh() -> None:
        nonlocal total_upserted, all_errors

        # ----- 1. Docs -----
        crawler = DocsCrawler(writer, config.sources, config.chunking)
        try:
            raw_text = await crawler.fetch_llms_txt()
        except Exception as exc:
            all_errors.append(f"Failed to fetch llms-full.txt: {exc}")
            raw_text = None

        if raw_text is not None:
            content_hash = hashlib.sha256(raw_text.encode()).hexdigest()
            stored_hash = index_store.get_metadata("docs:content_hash")
            if not force and stored_hash == content_hash:
                logger.info("Docs unchanged (hash=%s…), skipping", content_hash[:8])
            else:
                await index_store.delete_by_content_type("doc")
                docs_result = await crawler.ingest()
                total_upserted += docs_result.records_upserted
                all_errors.extend(docs_result.errors)
                logger.info(
                    "Docs crawl: upserted=%d errors=%d",
                    docs_result.records_upserted,
                    len(docs_result.errors),
                )
                if not docs_result.errors:
                    index_store.set_metadata("docs:content_hash", content_hash)
        await crawler.close()

        # ----- 2. Repos (code + source) -----
        github = GitHubRepoIngester(config, writer)
        changed_repos: list[str] = []
        repo_shas: dict[str, str] = {}
        prefetched: dict[str, tuple[Path, str]] = {}

        for repo_slug in config.sources.effective_repos:
            try:
                repo_path, commit_sha = await asyncio.to_thread(
                    github.clone_or_fetch, repo_slug
                )
                repo_shas[repo_slug] = commit_sha
                prefetched[repo_slug] = (repo_path, commit_sha)
            except Exception as exc:
                all_errors.append(f"Failed to clone/fetch {repo_slug}: {exc}")
                continue

            stored_sha = index_store.get_metadata(f"repo:{repo_slug}:commit_sha")
            if not force and stored_sha == commit_sha:
                logger.info(
                    "Repo %s unchanged (sha=%s…), skipping",
                    repo_slug,
                    commit_sha[:8],
                )
            else:
                changed_repos.append(repo_slug)

        # Delete and re-ingest only changed repos
        for repo_slug in changed_repos:
            await index_store.delete_by_repo(repo_slug)
            logger.info("Deleted stale records for %s", repo_slug)

        ingested_repos: set[str] = set()
        for repo_slug in changed_repos:
            repo_has_errors = False

            # Code ingest (per-repo for error tracking)
            code_result = await github.ingest(
                repos=[repo_slug], prefetched=prefetched,
            )
            total_upserted += code_result.records_upserted
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

            if not repo_has_errors:
                ingested_repos.add(repo_slug)

        # Store SHAs: unchanged repos (handles first-run) + successfully ingested repos
        for repo_slug, sha in repo_shas.items():
            if repo_slug not in changed_repos or repo_slug in ingested_repos:
                index_store.set_metadata(f"repo:{repo_slug}:commit_sha", sha)

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

    if not all_errors:
        index_store.set_metadata("last_refresh_at", now)
    else:
        index_store.set_metadata("last_refresh_errored_at", now)

    click.echo(f"Refresh complete: {total_upserted} records upserted in {duration}s.")
