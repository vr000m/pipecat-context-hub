"""CLI entry point for the Pipecat Context Hub.

Provides ``serve`` (default) and ``refresh`` commands.
"""

from __future__ import annotations

import asyncio
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
@click.pass_context
def refresh(ctx: click.Context) -> None:
    """Trigger a full index rebuild via the Ingester interface."""
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

    logger.info("Starting index refresh")
    start = time.monotonic()

    # Build the ingestion pipeline
    index_store = IndexStore(config.storage)
    embedding_svc = EmbeddingService(config.embedding)
    writer = EmbeddingIndexWriter(index_store, embedding_svc)

    total_upserted = 0
    all_errors: list[str] = []

    async def _run_refresh() -> None:
        nonlocal total_upserted, all_errors

        # Design decision: each content type is deleted BEFORE its ingester
        # runs.  If ingestion then fails, that type stays empty until the
        # next successful refresh.  This is intentional — for an LLM context
        # server, serving stale/outdated records is worse than serving none,
        # because stale context silently misleads the model.  A failed
        # refresh is visible in logs and the CLI exit message.

        # 1. Crawl docs — clear stale doc records first
        await index_store.delete_by_content_type("doc")
        crawler = DocsCrawler(writer, config.sources, config.chunking)
        try:
            docs_result = await crawler.ingest()
            total_upserted += docs_result.records_upserted
            all_errors.extend(docs_result.errors)
            logger.info(
                "Docs crawl: upserted=%d errors=%d",
                docs_result.records_upserted,
                len(docs_result.errors),
            )
        finally:
            await crawler.close()

        # 2. Ingest GitHub repos — clear stale code records first
        await index_store.delete_by_content_type("code")
        github = GitHubRepoIngester(config, writer)
        github_result = await github.ingest()
        total_upserted += github_result.records_upserted
        all_errors.extend(github_result.errors)
        logger.info(
            "GitHub ingest: upserted=%d errors=%d",
            github_result.records_upserted,
            len(github_result.errors),
        )

        # 3. Ingest pipecat source API — clear stale source records first
        await index_store.delete_by_content_type("source")
        source_ingester = SourceIngester(config, writer)
        source_result = await source_ingester.ingest()
        total_upserted += source_result.records_upserted
        all_errors.extend(source_result.errors)
        logger.info(
            "Source ingest: upserted=%d errors=%d",
            source_result.records_upserted,
            len(source_result.errors),
        )

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
    # last_refresh_at is only written on fully successful refreshes (0 errors)
    # so that get_hub_status accurately reports index health.
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
