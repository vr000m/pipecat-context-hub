"""CLI entry point for the Pipecat Context Hub.

Provides ``serve`` (default) and ``refresh`` commands.
"""

from __future__ import annotations

import asyncio
import logging
import sys

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
    ctx.obj["config"] = HubConfig(server=HubConfig().server.model_copy(update={"log_level": log_level}))
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve)


@main.command()
@click.pass_context
def serve(ctx: click.Context) -> None:
    """Start the MCP server (stdio transport)."""
    from pipecat_context_hub.server.main import create_server
    from pipecat_context_hub.server.transport import serve_stdio
    from pipecat_context_hub.server._stub_retriever import StubRetriever

    config: HubConfig = ctx.obj["config"]
    logging.getLogger(__name__).info(
        "Starting server with transport=%s", config.server.transport
    )
    retriever = StubRetriever()
    server = create_server(retriever)
    serve_stdio(server)


@main.command()
@click.pass_context
def refresh(ctx: click.Context) -> None:
    """Trigger a full index rebuild via the Ingester interface."""
    from pipecat_context_hub.server._stub_ingester import StubIngester

    logger = logging.getLogger(__name__)
    logger.info("Starting index refresh")
    ingester = StubIngester()
    result = asyncio.run(ingester.refresh())
    logger.info(
        "Refresh complete: source=%s upserted=%d deleted=%d errors=%d",
        result.source,
        result.records_upserted,
        result.records_deleted,
        len(result.errors),
    )
    click.echo(f"Refresh complete: {result.records_upserted} records upserted.")
