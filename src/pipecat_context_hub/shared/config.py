"""Configuration models for the Pipecat Context Hub.

Defines chunking policies, embedding settings, storage paths, and server config.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, computed_field, model_validator

logger = logging.getLogger(__name__)

# Environment variable for adding extra repos (comma-separated).
_EXTRA_REPOS_ENV = "PIPECAT_HUB_EXTRA_REPOS"

# Environment variable for skipping entire tainted repos (comma-separated).
_TAINTED_REPOS_ENV = "PIPECAT_HUB_TAINTED_REPOS"

# Environment variable for skipping specific tainted refs.
# Format: org/repo@ref,org/repo@other-ref
_TAINTED_REFS_ENV = "PIPECAT_HUB_TAINTED_REFS"

# Environment variable for enabling cross-encoder reranking.
_RERANKER_ENABLED_ENV = "PIPECAT_HUB_RERANKER_ENABLED"

# Environment variable for selecting the cross-encoder reranker model.
_RERANKER_MODEL_ENV = "PIPECAT_HUB_RERANKER_MODEL"

# Default cross-encoder model used when no override is supplied.
_DEFAULT_RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# Allowlisted cross-encoder models. Kept in the shared config layer so both
# the config resolver and the reranker service import it from the same place
# (single source of truth, upward dependency direction).
_ALLOWED_RERANKER_MODELS: frozenset[str] = frozenset({
    _DEFAULT_RERANKER_MODEL,
    "cross-encoder/ms-marco-MiniLM-L-12-v2",
    "cross-encoder/ms-marco-TinyBERT-L-2-v2",
})

# Environment variable for pinning the framework repo to a specific git tag.
_FRAMEWORK_VERSION_ENV = "PIPECAT_HUB_FRAMEWORK_VERSION"

# `serve` lifetime knobs. Idle timeout is user-facing (default 30 min;
# 0 disables). Parent-watch interval is hidden / for tests, but lives
# here for consistency with how every other env var is resolved.
_IDLE_TIMEOUT_ENV = "PIPECAT_HUB_IDLE_TIMEOUT_SECS"
_DEFAULT_IDLE_TIMEOUT_SECS = 1800.0
_PARENT_WATCH_INTERVAL_ENV = "PIPECAT_HUB_PARENT_WATCH_INTERVAL"
_DEFAULT_PARENT_WATCH_INTERVAL = 2.0
# Floor for the parent-watch interval when non-zero — prevents a
# misconfigured tiny value from CPU-spinning on os.getppid().
_PARENT_WATCH_INTERVAL_FLOOR = 0.1


def _split_csv_env(raw: str) -> list[str]:
    """Split a comma-separated env var into trimmed non-empty entries."""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """Deduplicate entries while preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _parse_tainted_refs(raw: str) -> dict[str, list[str]]:
    """Parse org/repo@ref entries from an env var.

    Malformed entries are ignored rather than raising at config-load time.
    """
    parsed: dict[str, list[str]] = {}
    for entry in _split_csv_env(raw):
        if "@" not in entry:
            continue
        repo_slug, ref = entry.rsplit("@", 1)
        repo_slug = repo_slug.strip()
        ref = ref.strip()
        if not repo_slug or not ref:
            continue
        refs = parsed.setdefault(repo_slug, [])
        if ref not in refs:
            refs.append(ref)
    return parsed


class ChunkingConfig(BaseModel):
    """Chunking policies for docs vs code."""

    doc_max_tokens: int = Field(default=512, description="Max tokens per doc chunk.")
    doc_overlap_tokens: int = Field(default=50, description="Token overlap between doc chunks.")
    code_max_tokens: int = Field(default=256, description="Max tokens per code chunk.")
    code_overlap_tokens: int = Field(default=25, description="Token overlap between code chunks.")
    code_prefer_function_boundaries: bool = Field(
        default=True,
        description="Try to split code at function/class boundaries when possible.",
    )


class EmbeddingConfig(BaseModel):
    """Embedding model settings."""

    model_name: str = Field(
        default="all-MiniLM-L6-v2",
        description="Sentence-transformers model for local embeddings.",
    )
    dimension: int = Field(default=384, description="Embedding vector dimension.")


class StorageConfig(BaseModel):
    """Local storage paths."""

    data_dir: Path = Field(
        default=Path.home() / ".pipecat-context-hub",
        description="Root directory for all local data.",
    )
    sqlite_filename: str = Field(default="metadata.db", description="SQLite database filename.")
    chroma_dirname: str = Field(default="chroma", description="ChromaDB persistence directory.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlite_path(self) -> Path:
        """Full path to SQLite database. Included in model_dump()/JSON."""
        return self.data_dir / self.sqlite_filename

    @computed_field  # type: ignore[prop-decorator]
    @property
    def chroma_path(self) -> Path:
        """Full path to ChromaDB directory. Included in model_dump()/JSON."""
        return self.data_dir / self.chroma_dirname


class RerankerConfig(BaseModel):
    """Cross-encoder reranking settings.

    Enable via ``PIPECAT_HUB_RERANKER_ENABLED=1`` environment variable or
    by setting ``enabled=True`` in Python. Disabled by default.
    """

    enabled: bool = Field(
        default=True,
        description="Enable cross-encoder reranking (adds ~50-100ms latency). "
        "Set PIPECAT_HUB_RERANKER_ENABLED=0 to disable via env var.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_enabled(self) -> bool:
        """Check both the field and the environment variable.

        The env var overrides the field in both directions:
        ``PIPECAT_HUB_RERANKER_ENABLED=0`` disables even if ``enabled=True``.
        """
        env = os.environ.get(_RERANKER_ENABLED_ENV, "").strip().lower()
        if env in ("0", "false", "no"):
            return False
        if env in ("1", "true", "yes"):
            return True
        return self.enabled
    cross_encoder_model: str = Field(
        default=_DEFAULT_RERANKER_MODEL,
        description="Cross-encoder model name from sentence-transformers. "
        "Override via PIPECAT_HUB_RERANKER_MODEL env var.",
    )
    top_n: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Number of top candidates to score with cross-encoder.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_model(self) -> str:
        """Resolve the active reranker model.

        Precedence: ``PIPECAT_HUB_RERANKER_MODEL`` env var >
        ``cross_encoder_model`` field > hardcoded default. Invalid values
        at either layer silently fall back — the server never fails to
        boot on a misconfigured env var or field. Warnings for invalid
        configuration are emitted once at construction time by
        ``_warn_on_invalid_model`` (a ``model_validator``); this property
        is a pure derivation so it is safe to call repeatedly (e.g. from
        ``model_dump()``).
        """
        env_value = os.environ.get(_RERANKER_MODEL_ENV, "").strip()
        if env_value and env_value in _ALLOWED_RERANKER_MODELS:
            return env_value
        if self.cross_encoder_model in _ALLOWED_RERANKER_MODELS:
            return self.cross_encoder_model
        return _DEFAULT_RERANKER_MODEL

    @model_validator(mode="after")
    def _warn_on_invalid_model(self) -> "RerankerConfig":
        """Emit configuration warnings exactly once, at construction time.

        Keeps ``effective_model`` free of side effects so the property is
        safe for repeated access during serialization.
        """
        import logging

        log = logging.getLogger(__name__)
        allowed_list = ", ".join(sorted(_ALLOWED_RERANKER_MODELS))

        env_value = os.environ.get(_RERANKER_MODEL_ENV, "").strip()

        # Compute the true fallback target so warnings name the real value.
        if self.cross_encoder_model in _ALLOWED_RERANKER_MODELS:
            fallback = self.cross_encoder_model
        else:
            fallback = _DEFAULT_RERANKER_MODEL

        if env_value and env_value not in _ALLOWED_RERANKER_MODELS:
            log.warning(
                "Unknown %s value '%s' — falling back to '%s'. Allowed: %s",
                _RERANKER_MODEL_ENV,
                env_value,
                fallback,
                allowed_list,
            )
        if self.cross_encoder_model not in _ALLOWED_RERANKER_MODELS:
            log.warning(
                "RerankerConfig.cross_encoder_model '%s' is not allowlisted — "
                "using default '%s'. Allowed: %s",
                self.cross_encoder_model,
                _DEFAULT_RERANKER_MODEL,
                allowed_list,
            )
        return self


class ServerConfig(BaseModel):
    """MCP server settings."""

    transport: Literal["stdio"] = Field(default="stdio", description="Transport type (stdio only in v0).")
    log_level: str = Field(default="INFO", description="Logging level.")
    idle_timeout_secs: float = Field(
        default=_DEFAULT_IDLE_TIMEOUT_SECS,
        description=(
            "Exit `serve` if no MCP request arrives for this many seconds. "
            "Catches the orphan-process case where the parent-death watchdog "
            "cannot fire (e.g. under `uv run`, where uv stays alive as an "
            "intermediate parent). Override via PIPECAT_HUB_IDLE_TIMEOUT_SECS. "
            "Set to 0 to disable."
        ),
    )
    parent_watch_interval_secs: float = Field(
        default=_DEFAULT_PARENT_WATCH_INTERVAL,
        description=(
            "Polling interval for the parent-death watchdog inside `serve`. "
            "Hidden tuning knob — for tests. Override via "
            "PIPECAT_HUB_PARENT_WATCH_INTERVAL. Set to 0 to disable the "
            "watchdog entirely."
        ),
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_idle_timeout_secs(self) -> float:
        """Resolved idle timeout: env var > field > default. 0 disables."""
        raw = os.environ.get(_IDLE_TIMEOUT_ENV, "").strip()
        if not raw:
            return max(0.0, self.idle_timeout_secs)
        try:
            parsed = float(raw)
        except ValueError:
            logger.warning(
                "Ignoring invalid %s=%r (not a float); using %.0fs",
                _IDLE_TIMEOUT_ENV,
                raw,
                self.idle_timeout_secs,
            )
            return max(0.0, self.idle_timeout_secs)
        if not math.isfinite(parsed):
            logger.warning(
                "Ignoring non-finite %s=%r; using %.0fs",
                _IDLE_TIMEOUT_ENV,
                raw,
                self.idle_timeout_secs,
            )
            return max(0.0, self.idle_timeout_secs)
        return max(0.0, parsed)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_parent_watch_interval_secs(self) -> float:
        """Resolved watchdog interval: env var > field > default.

        0 disables the watchdog. Non-zero values are floored at
        `_PARENT_WATCH_INTERVAL_FLOOR` to prevent a misconfigured tiny
        value (e.g. ``0.0001``) from CPU-spinning on ``os.getppid()``.
        """
        raw = os.environ.get(_PARENT_WATCH_INTERVAL_ENV, "").strip()
        if not raw:
            value: float = max(0.0, self.parent_watch_interval_secs)
        else:
            try:
                parsed = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid %s=%r (not a float); using %.1fs",
                    _PARENT_WATCH_INTERVAL_ENV,
                    raw,
                    self.parent_watch_interval_secs,
                )
                value = max(0.0, self.parent_watch_interval_secs)
            else:
                if not math.isfinite(parsed):
                    logger.warning(
                        "Ignoring non-finite %s=%r; using %.1fs",
                        _PARENT_WATCH_INTERVAL_ENV,
                        raw,
                        self.parent_watch_interval_secs,
                    )
                    value = max(0.0, self.parent_watch_interval_secs)
                else:
                    value = max(0.0, parsed)
        if value == 0.0:
            return 0.0
        return max(value, _PARENT_WATCH_INTERVAL_FLOOR)


class SourceConfig(BaseModel):
    """Source repositories and docs URL.

    Extra repos can be added via the ``PIPECAT_HUB_EXTRA_REPOS`` environment
    variable (comma-separated slugs, e.g.
    ``PIPECAT_HUB_EXTRA_REPOS="vr000m/decartai-sidekick,vr000m/pipecat-mcp-server"``).
    They are appended to the default repos list.

    Entire repos can be skipped via ``PIPECAT_HUB_TAINTED_REPOS`` and
    specific upstream refs can be skipped via ``PIPECAT_HUB_TAINTED_REFS``
    using ``org/repo@ref`` entries where ``ref`` is a tag or commit SHA/prefix.
    """

    docs_url: str = Field(
        default="https://docs.pipecat.ai/",
        description="Base docs URL (used as canonical source identifier).",
    )
    docs_llms_txt_url: str = Field(
        default="https://docs.pipecat.ai/llms-full.txt",
        description="URL for the pre-rendered llms-full.txt docs file.",
    )
    repos: list[str] = Field(
        default=[
            "pipecat-ai/pipecat",
            "pipecat-ai/pipecat-examples",
            "daily-co/daily-python",
            # Core TypeScript SDKs
            "pipecat-ai/pipecat-client-web",
            "pipecat-ai/pipecat-client-web-transports",
            "pipecat-ai/voice-ui-kit",
            "pipecat-ai/pipecat-flows-editor",
            "pipecat-ai/web-client-ui",
            "pipecat-ai/small-webrtc-prebuilt",
        ],
        description="GitHub repos to ingest.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_repos(self) -> list[str]:
        """Repos list with extra repos appended and tainted repos removed."""
        result = _dedupe_preserve_order(
            list(self.repos) + _split_csv_env(os.environ.get(_EXTRA_REPOS_ENV, ""))
        )
        tainted = set(self.tainted_repos)
        return [slug for slug in result if slug not in tainted]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tainted_repos(self) -> list[str]:
        """Repos explicitly blocked from refresh by local policy."""
        return _dedupe_preserve_order(
            _split_csv_env(os.environ.get(_TAINTED_REPOS_ENV, ""))
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tainted_refs_by_repo(self) -> dict[str, list[str]]:
        """Mapping of repo slug to tainted upstream refs to skip."""
        return _parse_tainted_refs(os.environ.get(_TAINTED_REFS_ENV, ""))


class HubConfig(BaseModel):
    """Top-level configuration for the Pipecat Context Hub."""

    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    sources: SourceConfig = Field(default_factory=SourceConfig)
    reranker: RerankerConfig = Field(default_factory=RerankerConfig)
    framework_version: str | None = Field(
        default=None,
        description="Pin the framework repo (pipecat-ai/pipecat) to a specific git tag "
        "(e.g. 'v0.0.96'). When set, source chunks come from that tag instead of HEAD. "
        "Set via --framework-version CLI flag or PIPECAT_HUB_FRAMEWORK_VERSION env var.",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def effective_framework_version(self) -> str | None:
        """Resolve framework version from field or env var.

        CLI flag (stored in ``framework_version``) takes precedence over env var.
        """
        if self.framework_version is not None:
            return self.framework_version
        env = os.environ.get(_FRAMEWORK_VERSION_ENV, "").strip()
        return env or None
