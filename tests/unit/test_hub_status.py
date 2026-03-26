"""Unit tests for the get_hub_status tool and index metadata persistence.

Tests cover:
1. FTSIndex metadata CRUD (set/get/get_all).
2. FTSIndex.get_index_stats with various content types.
3. handle_get_hub_status returns valid JSON with expected fields.
4. Hub status when no metadata exists (fresh install).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock

import pytest

from pipecat_context_hub.server.main import _SERVER_VERSION
from pipecat_context_hub.services.index.fts import FTSIndex
from pipecat_context_hub.shared.types import ChunkedRecord, HubStatusOutput


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 2, 26, tzinfo=timezone.utc)


@pytest.fixture
def fts_index(tmp_path):
    """Create an FTSIndex with a temporary database."""
    db_path = tmp_path / "metadata.db"
    index = FTSIndex(db_path)
    yield index
    index.close()


def _make_record(
    content_type: Literal["doc", "code", "readme", "source"],
    chunk_id: str,
    commit_sha: str | None = "abc123",
) -> ChunkedRecord:
    return ChunkedRecord(
        chunk_id=chunk_id,
        content=f"test content for {chunk_id}",
        content_type=content_type,
        source_url=f"https://example.com/{chunk_id}",
        path=f"test/{chunk_id}.py",
        commit_sha=commit_sha,
        indexed_at=NOW,
    )


# ---------------------------------------------------------------------------
# FTSIndex metadata tests
# ---------------------------------------------------------------------------


class TestFTSMetadata:
    def test_set_and_get_metadata(self, fts_index):
        fts_index.set_metadata("test_key", "test_value")
        assert fts_index.get_metadata("test_key") == "test_value"

    def test_get_metadata_missing_key(self, fts_index):
        assert fts_index.get_metadata("nonexistent") is None

    def test_set_metadata_upsert(self, fts_index):
        fts_index.set_metadata("key", "value1")
        fts_index.set_metadata("key", "value2")
        assert fts_index.get_metadata("key") == "value2"

    def test_get_all_metadata_empty(self, fts_index):
        assert fts_index.get_all_metadata() == {}

    def test_get_all_metadata_multiple(self, fts_index):
        fts_index.set_metadata("a", "1")
        fts_index.set_metadata("b", "2")
        fts_index.set_metadata("c", "3")
        result = fts_index.get_all_metadata()
        assert result == {"a": "1", "b": "2", "c": "3"}

    def test_metadata_persists_across_reopens(self, tmp_path):
        db_path = tmp_path / "metadata.db"
        index1 = FTSIndex(db_path)
        index1.set_metadata("persist_key", "persist_value")
        index1.close()

        index2 = FTSIndex(db_path)
        assert index2.get_metadata("persist_key") == "persist_value"
        index2.close()


# ---------------------------------------------------------------------------
# FTSIndex stats tests
# ---------------------------------------------------------------------------


class TestFTSIndexStats:
    def test_stats_empty_index(self, fts_index):
        stats = fts_index.get_index_stats()
        assert stats["total"] == 0
        assert stats["counts_by_type"] == {}
        assert stats["commit_shas"] == []

    def test_stats_single_type(self, fts_index):
        records = [_make_record("doc", f"d{i}") for i in range(3)]
        fts_index.upsert(records)
        stats = fts_index.get_index_stats()
        assert stats["total"] == 3
        assert stats["counts_by_type"] == {"doc": 3}

    def test_stats_multiple_types(self, fts_index):
        records = [
            _make_record("doc", "d1"),
            _make_record("doc", "d2"),
            _make_record("code", "c1"),
            _make_record("source", "s1"),
            _make_record("source", "s2"),
            _make_record("source", "s3"),
        ]
        fts_index.upsert(records)
        stats = fts_index.get_index_stats()
        assert stats["total"] == 6
        assert stats["counts_by_type"] == {"doc": 2, "code": 1, "source": 3}

    def test_stats_distinct_commit_shas(self, fts_index):
        records = [
            _make_record("doc", "d1", commit_sha="sha1"),
            _make_record("code", "c1", commit_sha="sha2"),
            _make_record("source", "s1", commit_sha="sha1"),  # duplicate
            _make_record("source", "s2", commit_sha=None),  # null
        ]
        fts_index.upsert(records)
        stats = fts_index.get_index_stats()
        assert sorted(stats["commit_shas"]) == ["sha1", "sha2"]

    def test_stats_after_delete(self, fts_index):
        records = [
            _make_record("doc", "d1"),
            _make_record("code", "c1"),
        ]
        fts_index.upsert(records)
        fts_index.delete_by_content_type("doc")
        stats = fts_index.get_index_stats()
        assert stats["total"] == 1
        assert stats["counts_by_type"] == {"code": 1}


# ---------------------------------------------------------------------------
# handle_get_hub_status tests
# ---------------------------------------------------------------------------


class TestHandleGetHubStatus:
    def _mock_index_store(
        self,
        stats: dict[str, Any] | None = None,
        metadata: dict[str, str] | None = None,
        data_dir: Path | None = None,
    ) -> MagicMock:
        store = MagicMock()
        store.get_index_stats.return_value = stats or {
            "total": 0,
            "counts_by_type": {},
            "commit_shas": [],
        }
        store.get_all_metadata.return_value = metadata or {}
        store.data_dir = data_dir or Path("/tmp/test")
        return store

    async def test_fresh_install_no_metadata(self):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        store = self._mock_index_store()
        result_json = await handle_get_hub_status({}, store)
        output = HubStatusOutput.model_validate_json(result_json)

        assert output.server_version == _SERVER_VERSION
        assert output.last_refresh_at is None
        assert output.last_refresh_duration_seconds is None
        assert output.total_records == 0
        assert output.counts_by_type == {}
        assert output.commit_shas == []

    async def test_with_refresh_metadata(self):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        store = self._mock_index_store(
            stats={
                "total": 10017,
                "counts_by_type": {"doc": 3520, "code": 1422, "source": 5075},
                "commit_shas": ["abc123", "def456"],
            },
            metadata={
                "last_refresh_at": "2026-02-26T10:00:00+00:00",
                "last_refresh_duration_seconds": "42.5",
                "last_refresh_records_upserted": "10017",
                "last_refresh_error_count": "0",
            },
        )
        result_json = await handle_get_hub_status({}, store)
        output = HubStatusOutput.model_validate_json(result_json)

        assert output.server_version == _SERVER_VERSION
        assert output.last_refresh_at == "2026-02-26T10:00:00+00:00"
        assert output.last_refresh_duration_seconds == 42.5
        assert output.total_records == 10017
        assert output.counts_by_type == {"doc": 3520, "code": 1422, "source": 5075}
        assert sorted(output.commit_shas) == ["abc123", "def456"]

    async def test_index_path_returned(self):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        store = self._mock_index_store(data_dir=Path("/home/user/.pipecat-context-hub"))
        result_json = await handle_get_hub_status({}, store)
        output = HubStatusOutput.model_validate_json(result_json)
        assert output.index_path == "/home/user/.pipecat-context-hub"

    async def test_returns_valid_json(self):
        from pipecat_context_hub.server.tools.get_hub_status import handle_get_hub_status

        store = self._mock_index_store()
        result_json = await handle_get_hub_status({}, store)
        parsed = json.loads(result_json)
        assert "server_version" in parsed
        assert "total_records" in parsed
        assert "counts_by_type" in parsed
