"""SQLite FTS5 keyword index for full-text search.

Stores ChunkedRecord content in an FTS5 virtual table for fast keyword
matching with BM25 ranking. A companion metadata table holds structured
fields for filtering.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from pipecat_context_hub.shared.types import ChunkedRecord, IndexQuery, IndexResult

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     TEXT PRIMARY KEY,
    content      TEXT NOT NULL,
    content_type TEXT NOT NULL,
    source_url   TEXT NOT NULL,
    repo         TEXT,
    path         TEXT NOT NULL,
    commit_sha   TEXT,
    indexed_at   TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    content='chunks',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS index_metadata (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Triggers to keep FTS in sync with the chunks table.
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES('delete', old.rowid, old.content);
    INSERT INTO chunks_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""


class FTSIndex:
    """SQLite FTS5 keyword index for full-text search.

    Maintains an FTS5 virtual table alongside a regular table for metadata
    filtering. Data persists in a SQLite database file on disk.
    """

    def __init__(self, sqlite_path: Path) -> None:
        self._sqlite_path = sqlite_path
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(sqlite_path), check_same_thread=False)
        self._closed = False
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        logger.info("FTSIndex initialized at %s", sqlite_path)

    def close(self) -> None:
        """Close the database connection."""
        if self._closed:
            return
        self._conn.close()
        self._closed = True

    def clear(self) -> None:
        """Delete all records from the chunks table (triggers sync FTS)."""
        self._conn.execute("DELETE FROM chunks")
        self._conn.commit()
        logger.info("FTSIndex cleared")

    def clear_metadata(self) -> None:
        """Delete all persisted index metadata."""
        self._conn.execute("DELETE FROM index_metadata")
        self._conn.commit()
        logger.info("FTSIndex metadata cleared")

    def reset(self) -> None:
        """Delete all indexed content and cached metadata."""
        self.clear()
        self.clear_metadata()
        logger.info("FTSIndex reset")

    def upsert(self, records: list[ChunkedRecord]) -> int:
        """Upsert records into the FTS index. Returns count written."""
        if not records:
            return 0

        count = 0
        for record in records:
            meta_json = json.dumps(record.metadata)
            self._conn.execute(
                """
                INSERT INTO chunks (chunk_id, content, content_type, source_url,
                                    repo, path, commit_sha, indexed_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_id) DO UPDATE SET
                    content = excluded.content,
                    content_type = excluded.content_type,
                    source_url = excluded.source_url,
                    repo = excluded.repo,
                    path = excluded.path,
                    commit_sha = excluded.commit_sha,
                    indexed_at = excluded.indexed_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    record.chunk_id,
                    record.content,
                    record.content_type,
                    record.source_url,
                    record.repo,
                    record.path,
                    record.commit_sha,
                    record.indexed_at.isoformat(),
                    meta_json,
                ),
            )
            count += 1

        self._conn.commit()
        logger.debug("Upserted %d records into FTS index", count)
        return count

    def delete_by_content_type(self, content_type: str) -> int:
        """Delete all records with a given content type. Returns count deleted."""
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE content_type = ?",
            (content_type,),
        )
        row = cursor.fetchone()
        count: int = row[0] if row else 0

        if count > 0:
            self._conn.execute(
                "DELETE FROM chunks WHERE content_type = ?",
                (content_type,),
            )
            self._conn.commit()
            logger.debug(
                "Deleted %d records from FTS index for content_type=%s", count, content_type
            )

        return count

    def delete_by_repo(self, repo: str) -> int:
        """Delete all records with a given repo. Returns count deleted."""
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE repo = ?",
            (repo,),
        )
        row = cursor.fetchone()
        count: int = row[0] if row else 0

        if count > 0:
            self._conn.execute(
                "DELETE FROM chunks WHERE repo = ?",
                (repo,),
            )
            self._conn.commit()
            logger.debug("Deleted %d records from FTS index for repo=%s", count, repo)

        return count

    def delete_by_source(self, source_url: str) -> int:
        """Delete all records with a given source URL. Returns count deleted."""
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE source_url = ?",
            (source_url,),
        )
        row = cursor.fetchone()
        count: int = row[0] if row else 0

        if count > 0:
            self._conn.execute(
                "DELETE FROM chunks WHERE source_url = ?",
                (source_url,),
            )
            self._conn.commit()
            logger.debug("Deleted %d records from FTS index for source %s", count, source_url)

        return count

    def search(self, query: IndexQuery) -> list[IndexResult]:
        """Search by keyword relevance using FTS5 BM25. Returns ranked results.

        When a ``chunk_id`` filter is present the search becomes a direct
        lookup and the FTS MATCH clause is skipped.
        """
        # Direct lookup by chunk_id — bypass FTS MATCH
        if "chunk_id" in query.filters:
            return self._get_by_chunk_id(query.filters["chunk_id"])

        if not query.query_text.strip():
            return []

        where_parts: list[str] = []
        params: list[Any] = []

        # FTS5 match
        where_parts.append("chunks_fts MATCH ?")
        params.append(self._sanitize_fts_query(query.query_text))

        # Metadata filters applied on the chunks table
        filter_clauses, filter_params = self._build_filter_sql(query.filters)
        where_parts.extend(filter_clauses)
        params.extend(filter_params)

        # `where_parts` only contains static clause templates; user input stays
        # parameterized in `params`, so joining the clauses is safe here.
        sql = "\n".join([
            "SELECT c.chunk_id, c.content, c.content_type, c.source_url,",
            "       c.repo, c.path, c.commit_sha, c.indexed_at, c.metadata_json,",
            "       bm25(chunks_fts) AS rank",
            "FROM chunks_fts",
            "JOIN chunks c ON c.rowid = chunks_fts.rowid",
            "WHERE " + " AND ".join(where_parts),
            "ORDER BY rank",
            "LIMIT ?",
        ])
        params.append(query.limit)

        cursor = self._conn.execute(sql, params)
        rows = cursor.fetchall()

        items: list[IndexResult] = []
        for row in rows:
            (
                chunk_id,
                content,
                content_type,
                source_url,
                repo,
                path,
                commit_sha,
                indexed_at_str,
                metadata_json,
                bm25_score,
            ) = row

            extra_meta: dict[str, Any] = json.loads(metadata_json) if metadata_json else {}

            record = ChunkedRecord(
                chunk_id=chunk_id,
                content=content,
                content_type=content_type,
                source_url=source_url,
                repo=repo,
                path=path,
                commit_sha=commit_sha,
                indexed_at=datetime.fromisoformat(indexed_at_str),
                metadata=extra_meta,
            )

            # BM25 returns negative scores (more negative = more relevant)
            # Convert to positive (higher = better)
            score = -float(bm25_score)

            items.append(
                IndexResult(
                    chunk=record,
                    score=score,
                    match_type="keyword",
                )
            )

        return items

    def _get_by_chunk_id(self, chunk_id: str) -> list[IndexResult]:
        """Direct lookup by chunk_id, bypassing FTS MATCH."""
        cursor = self._conn.execute(
            """
            SELECT chunk_id, content, content_type, source_url,
                   repo, path, commit_sha, indexed_at, metadata_json
            FROM chunks
            WHERE chunk_id = ?
            LIMIT 1
            """,
            (chunk_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return []

        (
            cid,
            content,
            content_type,
            source_url,
            repo,
            path,
            commit_sha,
            indexed_at_str,
            metadata_json,
        ) = row

        extra_meta: dict[str, Any] = json.loads(metadata_json) if metadata_json else {}
        record = ChunkedRecord(
            chunk_id=cid,
            content=content,
            content_type=content_type,
            source_url=source_url,
            repo=repo,
            path=path,
            commit_sha=commit_sha,
            indexed_at=datetime.fromisoformat(indexed_at_str),
            metadata=extra_meta,
        )
        return [IndexResult(chunk=record, score=1.0, match_type="keyword")]

    def set_metadata(self, key: str, value: str) -> None:
        """Upsert a key-value pair in the index_metadata table."""
        now = datetime.now().astimezone().isoformat()
        self._conn.execute(
            """
            INSERT INTO index_metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )
        self._conn.commit()

    def get_metadata(self, key: str) -> str | None:
        """Get a metadata value by key, or None if not found."""
        cursor = self._conn.execute("SELECT value FROM index_metadata WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else None

    def delete_metadata(self, key: str) -> None:
        """Remove a metadata key if it exists."""
        self._conn.execute("DELETE FROM index_metadata WHERE key = ?", (key,))
        self._conn.commit()

    def get_all_metadata(self) -> dict[str, str]:
        """Return all index_metadata as a dict."""
        cursor = self._conn.execute("SELECT key, value FROM index_metadata")
        return dict(cursor.fetchall())

    def get_counts_by_repo(self) -> dict[str, int]:
        """Return record counts grouped by repo. Includes a 'docs' key for doc chunks."""
        counts: dict[str, int] = {}
        # Doc chunks have no repo — count them separately
        cursor = self._conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE content_type = 'doc'"
        )
        doc_count = cursor.fetchone()[0]
        if doc_count:
            counts["docs.pipecat.ai"] = doc_count
        # Repo-scoped chunks
        cursor = self._conn.execute(
            "SELECT repo, COUNT(*) FROM chunks WHERE repo IS NOT NULL GROUP BY repo"
        )
        counts.update(dict(cursor.fetchall()))
        return counts

    def get_index_stats(self) -> dict[str, Any]:
        """Return record counts by content_type, total count, and distinct commit SHAs."""
        cursor = self._conn.execute(
            "SELECT content_type, COUNT(*) FROM chunks GROUP BY content_type"
        )
        counts_by_type: dict[str, int] = dict(cursor.fetchall())
        total: int = sum(counts_by_type.values())

        cursor = self._conn.execute(
            "SELECT DISTINCT commit_sha FROM chunks WHERE commit_sha IS NOT NULL"
        )
        commit_shas: list[str] = [r[0] for r in cursor.fetchall()]

        return {
            "counts_by_type": counts_by_type,
            "total": total,
            "commit_shas": commit_shas,
        }

    @staticmethod
    def _sanitize_fts_query(query_text: str) -> str:
        """Sanitize user query for FTS5 MATCH.

        Strips double quotes from tokens before wrapping in double quotes
        to prevent FTS5 syntax injection. Tokens are implicitly AND-ed.
        """
        tokens = query_text.split()
        # Strip quotes from each token to prevent FTS5 syntax breakout,
        # then wrap in double quotes (implicit AND in FTS5).
        return " ".join(
            f'"{token.replace(chr(34), "")}"'
            for token in tokens
            if token.strip() and token.replace('"', "").strip()
        )

    @staticmethod
    def _escape_like(value: str) -> str:
        """Escape LIKE metacharacters so they match literally."""
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _build_filter_sql(
        filters: dict[str, Any],
    ) -> tuple[list[str], list[Any]]:
        """Build SQL WHERE clauses for metadata filters.

        Returns (clause_list, param_list) to be appended to the WHERE.
        """
        esc = FTSIndex._escape_like
        clauses: list[str] = []
        params: list[Any] = []

        if "repo" in filters:
            clauses.append("c.repo = ?")
            params.append(filters["repo"])
        if "content_type" in filters:
            clauses.append("c.content_type = ?")
            params.append(filters["content_type"])
        if "path" in filters:
            clauses.append("c.path LIKE ? ESCAPE '\\'")
            params.append(f"{esc(filters['path'])}%")
        if "capability_tags" in filters:
            tags = filters["capability_tags"]
            if isinstance(tags, list):
                for tag in tags:
                    clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
                    params.append(f"%{esc(tag)}%")
            else:
                clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
                params.append(f"%{esc(tags)}%")
        # Metadata JSON filters for fields stored in the JSON blob
        for key in ("foundational_class", "language", "domain", "execution_mode"):
            if key in filters:
                clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
                params.append(f'%"{key}": "{esc(filters[key])}"%')
        # Source API metadata filters (exact match)
        for key in ("class_name", "chunk_type", "method_name"):
            if key in filters:
                clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
                params.append(f'%"{key}": "{esc(filters[key])}"%')
        # module_path is a prefix filter (e.g. "pipecat.services" matches "pipecat.services.tts")
        if "module_path" in filters:
            clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
            params.append(f'%"module_path": "{esc(filters["module_path"])}%')
        if "is_dataclass" in filters:
            val = "true" if filters["is_dataclass"] else "false"
            clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
            params.append(f'%"is_dataclass": {val}%')
        # Call-graph metadata filters — anchor to the JSON array key,
        # quote the value, and close with `]` to prevent matching across
        # field boundaries (e.g. empty "calls": [] followed by "method_name": "push_frame").
        if "yields" in filters:
            clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
            params.append(f'%"yields": [%"{esc(filters["yields"])}"%]%')
        if "calls" in filters:
            clauses.append("c.metadata_json LIKE ? ESCAPE '\\'")
            params.append(f'%"calls": [%"{esc(filters["calls"])}"%]%')

        return clauses, params
