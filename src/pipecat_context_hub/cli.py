"""CLI entry point for the Pipecat Context Hub.

Provides ``serve`` (default) and ``refresh`` commands.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

import click

from pipecat_context_hub.shared.config import HubConfig


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

    server = create_server(retriever)
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

        # 1. Crawl docs
        crawler = DocsCrawler(writer, config.sources, config.chunking)
        docs_result = await crawler.ingest()
        total_upserted += docs_result.records_upserted
        all_errors.extend(docs_result.errors)
        logger.info(
            "Docs crawl: upserted=%d errors=%d",
            docs_result.records_upserted,
            len(docs_result.errors),
        )

        # 2. Ingest GitHub repos
        github = GitHubRepoIngester(config, writer)
        github_result = await github.ingest()
        total_upserted += github_result.records_upserted
        all_errors.extend(github_result.errors)
        logger.info(
            "GitHub ingest: upserted=%d errors=%d",
            github_result.records_upserted,
            len(github_result.errors),
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

    click.echo(f"Refresh complete: {total_upserted} records upserted in {duration}s.")
