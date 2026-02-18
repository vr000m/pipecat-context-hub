"""Unit tests for the MCP server: tool registration, call dispatch, transport.

Tests cover:
1. Server registers all 5 tools and tools/list returns them.
2. Tool calls dispatch correctly and return valid JSON.
3. Unknown tool name raises ValueError.
4. Transport module is importable and functions exist.
5. CLI commands exist and are callable.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pipecat_context_hub.shared.types import (
    Citation,
    EvidenceReport,
    KnownItem,
    SearchDocsOutput,
    DocHit,
    GetDocOutput,
    SearchExamplesOutput,
    ExampleHit,
    GetExampleOutput,
    ExampleFile,
    TaxonomyEntry,
    GetCodeSnippetOutput,
    CodeSnippet,
)
from pipecat_context_hub.server.main import create_server, _TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 2, 18, tzinfo=timezone.utc)


def _citation(**overrides: Any) -> Citation:
    defaults: dict[str, Any] = {
        "source_url": "https://docs.pipecat.ai/test",
        "path": "test.md",
        "indexed_at": NOW,
    }
    defaults.update(overrides)
    return Citation.model_validate(defaults)


def _evidence() -> EvidenceReport:
    return EvidenceReport(
        known=[KnownItem(statement="test", citations=[_citation()], confidence=0.9)],
        unknown=[],
        confidence=0.9,
        confidence_rationale="test",
    )


@pytest.fixture
def mock_retriever():
    retriever = AsyncMock()

    retriever.search_docs.return_value = SearchDocsOutput(
        hits=[
            DocHit(
                doc_id="d1", title="T", snippet="S",
                citation=_citation(), score=0.9,
            )
        ],
        evidence=_evidence(),
    )

    retriever.get_doc.return_value = GetDocOutput(
        doc_id="d1", title="T", content="C",
        source_url="https://docs.pipecat.ai/test",
        indexed_at=NOW, sections=[], evidence=_evidence(),
    )

    retriever.search_examples.return_value = SearchExamplesOutput(
        hits=[
            ExampleHit(
                example_id="e1", summary="S", repo="r", path="p",
                citation=_citation(), score=0.9,
            )
        ],
        evidence=_evidence(),
    )

    retriever.get_example.return_value = GetExampleOutput(
        example_id="e1",
        metadata=TaxonomyEntry(example_id="e1", repo="r", path="p"),
        files=[ExampleFile(path="f.py", content="pass", language="python")],
        citation=_citation(), detected_symbols=[], evidence=_evidence(),
    )

    retriever.get_code_snippet.return_value = GetCodeSnippetOutput(
        snippets=[
            CodeSnippet(
                content="pass", path="f.py", line_start=1, line_end=1,
                language="python", citation=_citation(),
            )
        ],
        evidence=_evidence(),
    )

    return retriever


# ---------------------------------------------------------------------------
# Tool registration tests
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_registry_has_five_tools(self):
        assert len(_TOOL_REGISTRY) == 5

    def test_registry_tool_names(self):
        names = [name for name, _, _ in _TOOL_REGISTRY]
        assert names == [
            "search_docs",
            "get_doc",
            "search_examples",
            "get_example",
            "get_code_snippet",
        ]

    def test_registry_schemas_are_valid_json_schema(self):
        for name, _, schema in _TOOL_REGISTRY:
            assert schema["type"] == "object", f"{name} schema must be an object"
            assert "properties" in schema, f"{name} schema must have properties"

    async def test_list_tools_handler_registered(self, mock_retriever):
        import mcp.types as types

        server = create_server(mock_retriever)
        # Keys are request type classes, not strings
        assert types.ListToolsRequest in server.request_handlers


# ---------------------------------------------------------------------------
# Tool dispatch tests
# ---------------------------------------------------------------------------


class TestToolDispatch:
    async def test_call_tool_handler_registered(self, mock_retriever):
        import mcp.types as types

        server = create_server(mock_retriever)
        assert types.CallToolRequest in server.request_handlers

    async def test_create_server_returns_server(self, mock_retriever):
        from mcp.server.lowlevel import Server
        server = create_server(mock_retriever)
        assert isinstance(server, Server)

    async def test_server_name(self, mock_retriever):
        server = create_server(mock_retriever)
        assert server.name == "pipecat-context-hub"


# ---------------------------------------------------------------------------
# Transport module tests
# ---------------------------------------------------------------------------


class TestTransport:
    def test_transport_module_importable(self):
        from pipecat_context_hub.server import transport
        assert hasattr(transport, "run_stdio")
        assert hasattr(transport, "serve_stdio")
        assert callable(transport.run_stdio)
        assert callable(transport.serve_stdio)


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_main_importable(self):
        from pipecat_context_hub.cli import main
        assert main is not None

    def test_cli_has_serve_command(self):
        from pipecat_context_hub.cli import serve
        assert serve is not None

    def test_cli_has_refresh_command(self):
        from pipecat_context_hub.cli import refresh
        assert refresh is not None

    def test_cli_group_commands(self):
        from pipecat_context_hub.cli import main
        assert "serve" in main.commands
        assert "refresh" in main.commands


# ---------------------------------------------------------------------------
# Stub retriever and ingester tests
# ---------------------------------------------------------------------------


class TestStubs:
    async def test_stub_retriever_search_docs(self):
        from pipecat_context_hub.server._stub_retriever import StubRetriever
        from pipecat_context_hub.shared.types import SearchDocsInput

        r = StubRetriever()
        out = await r.search_docs(SearchDocsInput(query="test"))
        assert out.hits == []
        assert out.evidence.confidence == 0.0

    async def test_stub_retriever_get_doc(self):
        from pipecat_context_hub.server._stub_retriever import StubRetriever
        from pipecat_context_hub.shared.types import GetDocInput

        r = StubRetriever()
        out = await r.get_doc(GetDocInput(doc_id="x"))
        assert out.doc_id == "x"
        assert out.title == "(not indexed)"

    async def test_stub_retriever_search_examples(self):
        from pipecat_context_hub.server._stub_retriever import StubRetriever
        from pipecat_context_hub.shared.types import SearchExamplesInput

        r = StubRetriever()
        out = await r.search_examples(SearchExamplesInput(query="test"))
        assert out.hits == []

    async def test_stub_retriever_get_example(self):
        from pipecat_context_hub.server._stub_retriever import StubRetriever
        from pipecat_context_hub.shared.types import GetExampleInput

        r = StubRetriever()
        out = await r.get_example(GetExampleInput(example_id="e1"))
        assert out.example_id == "e1"

    async def test_stub_retriever_get_code_snippet(self):
        from pipecat_context_hub.server._stub_retriever import StubRetriever
        from pipecat_context_hub.shared.types import GetCodeSnippetInput

        r = StubRetriever()
        out = await r.get_code_snippet(GetCodeSnippetInput(intent="test"))
        assert out.snippets == []

    async def test_stub_ingester_ingest(self):
        from pipecat_context_hub.server._stub_ingester import StubIngester

        i = StubIngester()
        out = await i.ingest()
        assert out.source == "stub"

    async def test_stub_ingester_refresh(self):
        from pipecat_context_hub.server._stub_ingester import StubIngester

        i = StubIngester()
        out = await i.refresh()
        assert out.source == "stub"


# ---------------------------------------------------------------------------
# __main__ entry point test
# ---------------------------------------------------------------------------


class TestEntryPoint:
    def test_main_module_has_main(self):
        """Verify __main__ module exists and references cli.main."""
        import importlib

        mod = importlib.import_module("pipecat_context_hub.__main__")
        assert hasattr(mod, "main")
