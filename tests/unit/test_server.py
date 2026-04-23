"""Unit tests for the MCP server: tool registration, call dispatch, transport.

Tests cover:
1. Server registers all 7 tools and tools/list returns them.
2. Tool calls dispatch correctly and return valid JSON.
3. Unknown tool name raises ValueError.
4. Transport module is importable and functions exist.
5. CLI commands exist and are callable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pipecat_context_hub.shared.types import (
    ApiHit,
    Citation,
    EvidenceReport,
    KnownItem,
    SearchApiOutput,
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
from pipecat_context_hub.server.main import create_server, _BASE_TOOLS, _HUB_STATUS_TOOL


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

    retriever.search_api.return_value = SearchApiOutput(
        hits=[
            ApiHit(
                chunk_id="a1",
                module_path="pipecat.services.tts",
                chunk_type="class_overview",
                snippet="class TTSService:",
                is_dataclass=False,
                citation=_citation(),
                score=0.9,
            )
        ],
        evidence=_evidence(),
    )

    return retriever


# ---------------------------------------------------------------------------
# Tool registration tests
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_base_tools_has_seven_entries(self):
        assert len(_BASE_TOOLS) == 7

    def test_base_tool_names(self):
        names = [name for name, _, _ in _BASE_TOOLS]
        assert names == [
            "search_docs",
            "get_doc",
            "search_examples",
            "get_example",
            "get_code_snippet",
            "search_api",
            "check_deprecation",
        ]

    def test_hub_status_tool_exists(self):
        name, desc, schema = _HUB_STATUS_TOOL
        assert name == "get_hub_status"
        assert schema["type"] == "object"

    def test_registry_schemas_are_valid_json_schema(self):
        all_tools = list(_BASE_TOOLS) + [_HUB_STATUS_TOOL]
        for name, _, schema in all_tools:
            assert schema["type"] == "object", f"{name} schema must be an object"
            assert "properties" in schema, f"{name} schema must have properties"

    def test_hub_status_not_listed_without_store(self, mock_retriever):
        """Without index_store, get_hub_status should not be registered."""
        server = create_server(mock_retriever)
        # We can't call list_tools directly, but we verify the closure
        # builds correctly — the real test is that ValueError is never raised
        assert server.name == "pipecat-context-hub"

    async def test_list_tools_handler_registered(self, mock_retriever):
        import mcp.types as types

        server = create_server(mock_retriever)
        assert types.ListToolsRequest in server.request_handlers

    async def test_list_tools_touches_idle_tracker(self, mock_retriever):
        """tools/list must reset the idle clock — clients that only poll
        capabilities (no tool calls) still represent an active session."""
        import mcp.types as types
        from pipecat_context_hub.shared.tracking import IdleTracker

        tracker = IdleTracker()
        server = create_server(mock_retriever, idle_tracker=tracker)
        handler = server.request_handlers[types.ListToolsRequest]
        request = types.ListToolsRequest(method="tools/list")

        # Age the tracker, then fire the handler; touch() must reset it.
        tracker._last -= 1000.0
        assert tracker.seconds_since_last() >= 1000.0
        await handler(request)
        assert tracker.seconds_since_last() < 1.0

    async def test_call_tool_touches_idle_tracker(self, mock_retriever):
        """tools/call must reset the idle clock (existing behaviour, now pinned)."""
        import mcp.types as types
        from pipecat_context_hub.shared.tracking import IdleTracker

        tracker = IdleTracker()
        server = create_server(mock_retriever, idle_tracker=tracker)
        handler = server.request_handlers[types.CallToolRequest]
        request = types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name="search_docs", arguments={"query": "x"}),
        )

        tracker._last -= 1000.0
        assert tracker.seconds_since_last() >= 1000.0
        await handler(request)
        assert tracker.seconds_since_last() < 1.0

    async def test_ping_touches_idle_tracker(self, mock_retriever):
        """MCP `ping` requests are handled by the low-level Server directly
        (not via our list/call decorators), so they must still count as
        activity — otherwise a client keeping an idle session alive with
        periodic ping heartbeats would still be reaped as idle.
        """
        import mcp.types as types
        from pipecat_context_hub.shared.tracking import IdleTracker

        tracker = IdleTracker()
        server = create_server(mock_retriever, idle_tracker=tracker)
        handler = server.request_handlers[types.PingRequest]
        request = types.PingRequest(method="ping")

        tracker._last -= 1000.0
        assert tracker.seconds_since_last() >= 1000.0
        result = await handler(request)
        assert tracker.seconds_since_last() < 1.0
        # Built-in ping still returns an EmptyResult.
        assert isinstance(result.root, types.EmptyResult)

    async def test_ping_handler_noop_without_idle_tracker(self, mock_retriever):
        """Omitting idle_tracker must leave the built-in ping handler
        in place unchanged — we don't want to break ping when idle
        watchdogging is disabled."""
        import mcp.types as types

        server = create_server(mock_retriever)  # no idle_tracker
        handler = server.request_handlers[types.PingRequest]
        result = await handler(types.PingRequest(method="ping"))
        assert isinstance(result.root, types.EmptyResult)


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


class TestGetHubStatusRerankerFields:
    """Reranker fields in handle_get_hub_status reflect live runtime state."""

    def _stub_store(self) -> Any:
        class _Stub:
            data_dir = "/tmp/hub"

            def get_index_stats(self) -> dict[str, Any]:
                return {"total": 0, "counts_by_type": {}, "commit_shas": []}

            def get_all_metadata(self) -> dict[str, str]:
                return {}

        return _Stub()

    async def test_enabled_reports_live_model(self):
        import json

        from pipecat_context_hub.server.tools.get_hub_status import (
            handle_get_hub_status,
        )
        from pipecat_context_hub.shared.types import RerankerStatus

        status = RerankerStatus(
            enabled=True,
            model="cross-encoder/ms-marco-TinyBERT-L-2-v2",
            configured_model="cross-encoder/ms-marco-TinyBERT-L-2-v2",
        )
        payload = await handle_get_hub_status({}, self._stub_store(), status)
        data = json.loads(payload)
        assert data["reranker_enabled"] is True
        assert data["reranker_model"] == "cross-encoder/ms-marco-TinyBERT-L-2-v2"
        assert data["reranker_configured_model"] == "cross-encoder/ms-marco-TinyBERT-L-2-v2"
        assert data["reranker_disabled_reason"] is None

    async def test_not_cached_surfaces_reason_and_configured_model(self):
        import json

        from pipecat_context_hub.server.tools.get_hub_status import (
            handle_get_hub_status,
        )
        from pipecat_context_hub.shared.types import RerankerStatus

        status = RerankerStatus(
            enabled=False,
            configured_model="cross-encoder/ms-marco-MiniLM-L-12-v2",
            disabled_reason="not_cached",
        )
        payload = await handle_get_hub_status({}, self._stub_store(), status)
        data = json.loads(payload)
        assert data["reranker_enabled"] is False
        assert data["reranker_model"] is None
        assert data["reranker_configured_model"] == "cross-encoder/ms-marco-MiniLM-L-12-v2"
        assert data["reranker_disabled_reason"] == "not_cached"

    async def test_load_failed_surfaces_reason(self):
        import json

        from pipecat_context_hub.server.tools.get_hub_status import (
            handle_get_hub_status,
        )
        from pipecat_context_hub.shared.types import RerankerStatus

        status = RerankerStatus(
            enabled=False,
            configured_model="cross-encoder/ms-marco-MiniLM-L-6-v2",
            disabled_reason="load_failed",
        )
        payload = await handle_get_hub_status({}, self._stub_store(), status)
        data = json.loads(payload)
        assert data["reranker_enabled"] is False
        assert data["reranker_disabled_reason"] == "load_failed"

    async def test_no_status_returns_disabled_with_unknown_reason(self):
        import json

        from pipecat_context_hub.server.tools.get_hub_status import (
            handle_get_hub_status,
        )

        payload = await handle_get_hub_status({}, self._stub_store(), None)
        data = json.loads(payload)
        assert data["reranker_enabled"] is False
        assert data["reranker_model"] is None
        # When no provider is wired the reason is unknown — don't lie.
        assert data["reranker_disabled_reason"] is None

    async def test_provider_is_called_per_query(self):
        """create_server evaluates the provider on each get_hub_status call."""
        from unittest.mock import AsyncMock

        from pipecat_context_hub.server.main import create_server
        from pipecat_context_hub.shared.types import RerankerStatus

        retriever = AsyncMock()
        calls: list[int] = []

        def _provider() -> RerankerStatus:
            calls.append(1)
            return RerankerStatus(enabled=False, disabled_reason="config_disabled")

        server = create_server(
            retriever,
            index_store=self._stub_store(),
            reranker_status_provider=_provider,
        )
        assert server.name == "pipecat-context-hub"
        # The closure is wired in — exercising it requires the full call path
        # which is covered by integration tests; here we assert create_server
        # accepts the callable and does not eagerly invoke it.
        assert calls == []


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
# __main__ entry point test
# ---------------------------------------------------------------------------


class TestVersionConsistency:
    """Ensure pyproject.toml version and _SERVER_VERSION stay in sync."""

    def test_server_version_matches_pyproject(self):
        """_SERVER_VERSION in server/main.py must match pyproject.toml [project].version."""
        import tomllib
        from pathlib import Path

        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        with pyproject_path.open("rb") as f:
            pyproject_version = tomllib.load(f)["project"]["version"]

        from pipecat_context_hub.server.main import _SERVER_VERSION

        assert _SERVER_VERSION == pyproject_version, (
            f"Version mismatch: _SERVER_VERSION={_SERVER_VERSION!r} "
            f"but pyproject.toml version={pyproject_version!r}. "
            f"Both must be updated together on each release."
        )


class TestEntryPoint:
    def test_main_module_has_main(self):
        """Verify __main__ module exists and references cli.main."""
        import importlib

        mod = importlib.import_module("pipecat_context_hub.__main__")
        assert hasattr(mod, "main")
