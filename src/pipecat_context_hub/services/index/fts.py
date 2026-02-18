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
        self._conn = sqlite3.connect(str(sqlite_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.commit()
        logger.info("FTSIndex initialized at %s", sqlite_path)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

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

        where_sql = " AND ".join(where_parts)

        sql = f"""
            SELECT c.chunk_id, c.content, c.content_type, c.source_url,
                   c.repo, c.path, c.commit_sha, c.indexed_at, c.metadata_json,
                   bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.rowid = chunks_fts.rowid
            WHERE {where_sql}
            ORDER BY rank
            LIMIT ?
        """
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
            cid, content, content_type, source_url,
            repo, path, commit_sha, indexed_at_str, metadata_json,
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

    @staticmethod
    def _sanitize_fts_query(query_text: str) -> str:
        """Sanitize user query for FTS5 MATCH.

        Wraps each token in double quotes to prevent FTS5 syntax errors
        from special characters. Tokens are implicitly AND-ed.
        """
        tokens = query_text.split()
        # Quote each token and join with spaces (implicit AND in FTS5)
        return " ".join(f'"{token}"' for token in tokens if token.strip())

    @staticmethod
    def _build_filter_sql(
        filters: dict[str, Any],
    ) -> tuple[list[str], list[Any]]:
        """Build SQL WHERE clauses for metadata filters.

        Returns (clause_list, param_list) to be appended to the WHERE.
        """
        clauses: list[str] = []
        params: list[Any] = []

        if "repo" in filters:
            clauses.append("c.repo = ?")
            params.append(filters["repo"])
        if "content_type" in filters:
            clauses.append("c.content_type = ?")
            params.append(filters["content_type"])
        if "path" in filters:
            clauses.append("c.path LIKE ?")
            params.append(f"{filters['path']}%")
        if "capability_tags" in filters:
            tags = filters["capability_tags"]
            if isinstance(tags, list):
                for tag in tags:
                    clauses.append("c.metadata_json LIKE ?")
                    params.append(f"%{tag}%")
            else:
                clauses.append("c.metadata_json LIKE ?")
                params.append(f"%{tags}%")
        # Metadata JSON filters for fields stored in the JSON blob
        for key in ("foundational_class", "language", "execution_mode"):
            if key in filters:
                clauses.append("c.metadata_json LIKE ?")
                params.append(f'%"{key}": "{filters[key]}"%')

        return clauses, params
