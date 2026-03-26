"""Live retrieval-quality benchmark against the default indexed corpus.

Opt-in only:
  PIPECAT_HUB_ENABLE_QUALITY_BENCHMARK=1 \
    uv run pytest tests/benchmarks/test_retrieval_quality.py -v -s

Optional JSON report:
  PIPECAT_HUB_ENABLE_QUALITY_BENCHMARK=1 \
  PIPECAT_HUB_BENCHMARK_OUTPUT=artifacts/benchmarks/retrieval-quality.json \
    uv run pytest tests/benchmarks/test_retrieval_quality.py -v -s
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from pipecat_context_hub.server.main import _SERVER_VERSION
from pipecat_context_hub.services.embedding import EmbeddingService
from pipecat_context_hub.services.index.store import IndexStore
from pipecat_context_hub.services.retrieval.hybrid import HybridRetriever
from pipecat_context_hub.shared.config import HubConfig
from pipecat_context_hub.shared.types import (
    GetCodeSnippetInput,
    GetCodeSnippetOutput,
    SearchApiInput,
    SearchApiOutput,
    SearchDocsInput,
    SearchDocsOutput,
    SearchExamplesInput,
    SearchExamplesOutput,
)

_ENABLE_ENV = "PIPECAT_HUB_ENABLE_QUALITY_BENCHMARK"
_OUTPUT_ENV = "PIPECAT_HUB_BENCHMARK_OUTPUT"
_SCHEMA_VERSION = 1
_MATRIX_VERSION = "default-v1"
_DEFAULT_REPOS = ("pipecat-ai/pipecat", "pipecat-ai/pipecat-examples", "daily-co/daily-python")
_VECTOR_HEALTH_TIMEOUT_SECONDS = 15
_RECOVERY_COMMAND = "pipecat-context-hub refresh --force --reset-index"


@dataclass(frozen=True)
class QualityCase:
    """Definition of a live quality benchmark query."""

    name: str
    tool: Literal["search_docs", "search_examples", "search_api", "get_code_snippet"]
    input: SearchDocsInput | SearchExamplesInput | SearchApiInput | GetCodeSnippetInput
    alias_groups: tuple[tuple[str, ...], ...] = ()
    expected_repos: tuple[str, ...] = ()
    expected_path_fragments: tuple[str, ...] = ()
    preferred_chunk_types: tuple[str, ...] = ()
    informational: bool = False
    min_score: float = 0.67


_QUALITY_CASES: tuple[QualityCase, ...] = (
    QualityCase(
        name="docs_tts_stt",
        tool="search_docs",
        input=SearchDocsInput(query="TTS + STT", limit=5),
        alias_groups=(
            ("tts", "text-to-speech"),
            ("stt", "speech-to-text"),
        ),
    ),
    QualityCase(
        name="docs_function_calling_tools",
        tool="search_docs",
        input=SearchDocsInput(query="function calling + tools", limit=5),
        alias_groups=(
            ("function calling", "function-calling"),
            ("tool", "tools", "mcp"),
        ),
    ),
    QualityCase(
        name="api_pipeline_task_frame_processor",
        tool="search_api",
        input=SearchApiInput(query="PipelineTask + FrameProcessor", limit=5),
        alias_groups=(
            ("pipelinetask",),
            ("frameprocessor",),
        ),
        preferred_chunk_types=("method", "class_overview"),
    ),
    QualityCase(
        name="api_base_transport_websocket_transport",
        tool="search_api",
        input=SearchApiInput(query="BaseTransport + WebSocketTransport", limit=5),
        alias_groups=(
            ("basetransport",),
            ("websockettransport",),
        ),
        preferred_chunk_types=("method", "class_overview"),
    ),
    QualityCase(
        name="snippet_runner_daily_configure",
        tool="get_code_snippet",
        input=GetCodeSnippetInput(
            symbol="configure",
            module="pipecat.runner.daily",
            max_lines=80,
        ),
        alias_groups=(("configure(", "def configure"),),
        expected_repos=("pipecat-ai/pipecat",),
        expected_path_fragments=("runner/daily",),
    ),
    QualityCase(
        name="examples_wake_word_dailytransport",
        tool="search_examples",
        input=SearchExamplesInput(query="wake word + DailyTransport", limit=5),
        alias_groups=(
            ("wake word", "wake-word", "wake phrase"),
            ("dailytransport", "daily transport"),
        ),
        expected_repos=_DEFAULT_REPOS,
        informational=True,
        min_score=0.50,
    ),
    QualityCase(
        name="examples_function_calling_tools",
        tool="search_examples",
        input=SearchExamplesInput(query="function calling + tools", limit=5),
        alias_groups=(
            ("function calling", "function-calling"),
            ("tool", "tools"),
        ),
        expected_repos=_DEFAULT_REPOS,
        informational=True,
        min_score=0.50,
    ),
)


def _require_opt_in() -> None:
    """Skip unless the live quality benchmark was explicitly enabled."""
    if os.environ.get(_ENABLE_ENV, "").strip() != "1":
        pytest.skip(f"Set {_ENABLE_ENV}=1 to run the live retrieval-quality benchmark.")


def _repo_sha_map(metadata: dict[str, str]) -> dict[str, str]:
    """Extract repo slug → commit SHA from persisted index metadata."""
    repo_shas: dict[str, str] = {}
    suffix = ":commit_sha"
    for key, value in metadata.items():
        if key.startswith("repo:") and key.endswith(suffix):
            repo_shas[key[len("repo:") : -len(suffix)]] = value
    return dict(sorted(repo_shas.items()))


def _coverage_score(
    texts: list[str], alias_groups: tuple[tuple[str, ...], ...]
) -> tuple[float, list[str]]:
    """Measure concept coverage over a list of result texts."""
    if not alias_groups:
        return 1.0, []
    haystack = " ".join(texts).lower()
    matched: list[str] = []
    for group in alias_groups:
        for alias in group:
            if alias.lower() in haystack:
                matched.append(group[0])
                break
    return len(matched) / len(alias_groups), matched


def _mean(values: list[float]) -> float:
    """Return the average of non-empty values."""
    return sum(values) / len(values) if values else 0.0


def _probe_error_detail(stderr: str | bytes | None) -> str:
    """Return the last non-empty stderr line from a health probe."""
    if not stderr:
        return "no stderr captured"
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no stderr captured"


def _assert_vector_index_healthy(config: HubConfig) -> None:
    """Fail fast when the local Chroma index cannot complete a trivial query."""
    probe_code = (
        "import asyncio, sys; "
        "from pathlib import Path; "
        "from pipecat_context_hub.shared.config import StorageConfig; "
        "from pipecat_context_hub.services.index.store import IndexStore; "
        "from pipecat_context_hub.shared.types import IndexQuery; "
        "store = IndexStore(StorageConfig(data_dir=Path(sys.argv[1]))); "
        "query = IndexQuery(query_text='healthcheck', query_embedding=[0.0] * int(sys.argv[2]), filters={'content_type': 'doc'}, limit=1); "
        "ns = {'asyncio': asyncio, 'store': store, 'query': query}; "
        "exec('async def _m():\\n    try:\\n        await store.vector_search(query)\\n    finally:\\n        store.close()', ns); "
        "asyncio.run(ns['_m']())"
    )
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                probe_code,
                str(config.storage.data_dir),
                str(config.embedding.dimension),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_VECTOR_HEALTH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        detail = _probe_error_detail(exc.stderr)
        pytest.fail(
            "Local Chroma vector index health probe timed out after "
            f"{_VECTOR_HEALTH_TIMEOUT_SECONDS}s. "
            f"Run `{_RECOVERY_COMMAND}` and rerun the benchmark. "
            f"Last probe output: {detail}"
        )

    if completed.returncode != 0:
        detail = _probe_error_detail(completed.stderr)
        pytest.fail(
            "Local Chroma vector index health probe failed before the benchmark started. "
            f"Run `{_RECOVERY_COMMAND}` and rerun the benchmark. "
            f"Last probe output: {detail}"
        )


def _evaluate_search_docs(case: QualityCase, output: SearchDocsOutput) -> dict[str, Any]:
    hits = output.hits
    texts = [
        " ".join(
            filter(
                None,
                [
                    hit.title,
                    hit.section or "",
                    hit.snippet,
                    hit.citation.path,
                ],
            )
        )
        for hit in hits[:5]
    ]
    coverage, matched = _coverage_score(texts, case.alias_groups)
    parts = {
        "hits": 1.0 if hits else 0.0,
        "confidence": 0.0 if output.evidence.low_confidence else 1.0,
        "concepts": coverage,
    }
    return {
        "score": round(_mean(list(parts.values())), 2),
        "parts": parts,
        "matched_alias_groups": matched,
        "top_results": [f"{hit.title} [{hit.citation.path}]" for hit in hits[:3]],
    }


def _evaluate_search_examples(case: QualityCase, output: SearchExamplesOutput) -> dict[str, Any]:
    hits = output.hits
    texts = [
        " ".join(
            [
                hit.repo,
                hit.path,
                hit.summary,
                " ".join(hit.capability_tags),
            ]
        )
        for hit in hits[:5]
    ]
    coverage, matched = _coverage_score(texts, case.alias_groups)
    repo_matches = [
        hit.repo
        for hit in hits[:5]
        if not case.expected_repos or hit.repo in case.expected_repos
    ]
    repo_score = (len(repo_matches) / min(len(hits), 5)) if hits else 0.0
    parts = {
        "hits": 1.0 if hits else 0.0,
        "confidence": 0.0 if output.evidence.low_confidence else 1.0,
        "concepts": coverage,
        "repo_set": repo_score,
    }
    return {
        "score": round(_mean(list(parts.values())), 2),
        "parts": parts,
        "matched_alias_groups": matched,
        "top_results": [f"{hit.repo}:{hit.path}" for hit in hits[:3]],
    }


def _evaluate_search_api(case: QualityCase, output: SearchApiOutput) -> dict[str, Any]:
    hits = output.hits
    texts = [
        " ".join(
            filter(
                None,
                [
                    hit.module_path,
                    hit.class_name or "",
                    hit.method_name or "",
                    hit.snippet,
                ],
            )
        )
        for hit in hits[:5]
    ]
    coverage, matched = _coverage_score(texts, case.alias_groups)
    preferred = 0.0
    if case.preferred_chunk_types and hits:
        preferred_hits = [
            hit for hit in hits[:3] if hit.chunk_type in case.preferred_chunk_types
        ]
        preferred = len(preferred_hits) / min(len(hits), 3)
    parts = {
        "hits": 1.0 if hits else 0.0,
        "confidence": 0.0 if output.evidence.low_confidence else 1.0,
        "concepts": coverage,
        "preferred_chunks": preferred,
    }
    return {
        "score": round(_mean(list(parts.values())), 2),
        "parts": parts,
        "matched_alias_groups": matched,
        "top_results": [
            f"{hit.chunk_type}:{hit.module_path}:{hit.class_name or hit.method_name or hit.chunk_id}"
            for hit in hits[:3]
        ],
    }


def _evaluate_get_code_snippet(case: QualityCase, output: GetCodeSnippetOutput) -> dict[str, Any]:
    snippets = output.snippets
    texts = [
        " ".join([snippet.path, snippet.content])
        for snippet in snippets[:3]
    ]
    coverage, matched = _coverage_score(texts, case.alias_groups)
    repo_score = 0.0
    if snippets and case.expected_repos:
        repo_score = float(
            snippets[0].citation.repo is not None
            and snippets[0].citation.repo in case.expected_repos
        )
    path_score = 1.0
    if snippets and case.expected_path_fragments:
        path_lower = snippets[0].path.lower()
        path_score = float(
            any(fragment in path_lower for fragment in case.expected_path_fragments)
        )
    elif not snippets and case.expected_path_fragments:
        path_score = 0.0
    parts = {
        "hits": 1.0 if snippets else 0.0,
        "confidence": 0.0 if output.evidence.low_confidence else 1.0,
        "concepts": coverage,
        "repo": repo_score if case.expected_repos else 1.0,
        "path": path_score,
    }
    return {
        "score": round(_mean(list(parts.values())), 2),
        "parts": parts,
        "matched_alias_groups": matched,
        "top_results": [
            f"{snippet.path}:{snippet.line_start}-{snippet.line_end}" for snippet in snippets[:3]
        ],
    }


async def _run_case(
    case: QualityCase, retriever: HybridRetriever, *, strict_mode: bool
) -> dict[str, Any]:
    """Execute one quality case and return its scored result."""
    if case.tool == "search_docs":
        docs_output = await retriever.search_docs(cast(SearchDocsInput, case.input))
        result = _evaluate_search_docs(case, docs_output)
    elif case.tool == "search_examples":
        examples_output = await retriever.search_examples(cast(SearchExamplesInput, case.input))
        result = _evaluate_search_examples(case, examples_output)
    elif case.tool == "search_api":
        api_output = await retriever.search_api(cast(SearchApiInput, case.input))
        result = _evaluate_search_api(case, api_output)
    else:
        snippet_output = await retriever.get_code_snippet(cast(GetCodeSnippetInput, case.input))
        result = _evaluate_get_code_snippet(case, snippet_output)

    threshold_met = result["score"] >= case.min_score
    status = "PASS"
    if not threshold_met:
        status = "WARN" if case.informational or not strict_mode else "FAIL"

    result.update(
        {
            "name": case.name,
            "tool": case.tool,
            "informational": case.informational,
            "min_score": case.min_score,
            "threshold_met": threshold_met,
            "status": status,
            "input": case.input.model_dump(mode="json", exclude_none=True),
        }
    )
    return result


def _write_report(report: dict[str, Any]) -> None:
    """Persist a JSON report when requested via environment variable."""
    output_path = os.environ.get(_OUTPUT_ENV, "").strip()
    if not output_path:
        return

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\nWrote retrieval-quality report to {path}")


@pytest.fixture(scope="module")
def live_quality_context() -> Generator[dict[str, Any], None, None]:
    """Create a retriever against the current local index and capture corpus metadata."""
    _require_opt_in()

    config = HubConfig()
    _assert_vector_index_healthy(config)

    store = IndexStore(config.storage)
    stats = store.get_index_stats()
    if stats["total"] == 0:
        store.close()
        pytest.skip("Local index is empty. Run 'pipecat-context-hub refresh' first.")

    metadata = store.get_all_metadata()
    repo_counts = store.get_counts_by_repo()
    missing_default = [repo for repo in _DEFAULT_REPOS if repo_counts.get(repo, 0) == 0]
    if missing_default:
        store.close()
        pytest.fail(
            "Default corpus incomplete for retrieval benchmark. "
            f"Missing repo data for: {', '.join(missing_default)}. "
            "Run 'pipecat-context-hub refresh' with the default repos."
        )

    embedding = EmbeddingService(config.embedding)
    # Warm the embedding model once before multi-concept queries fan out
    # across threads; this avoids first-use lazy-load races in the benchmark.
    embedding.embed_query("warmup")
    retriever = HybridRetriever(store, embedding)
    repo_shas = _repo_sha_map(metadata)
    extra_repos = sorted(set(repo_shas) - set(_DEFAULT_REPOS))

    context = {
        "retriever": retriever,
        "store": store,
        "stats": stats,
        "metadata": metadata,
        "repo_counts": repo_counts,
        "repo_shas": repo_shas,
        "extra_repos": extra_repos,
        "docs_content_hash": metadata.get("docs:content_hash"),
    }

    yield context
    store.close()


@pytest.mark.benchmark
class TestRetrievalQuality:
    """Live quality benchmark for the default Pipecat corpus."""

    async def test_default_corpus_quality_summary(
        self, live_quality_context: dict[str, Any]
    ) -> None:
        retriever: HybridRetriever = live_quality_context["retriever"]
        strict_mode = not live_quality_context["extra_repos"]

        case_results: list[dict[str, Any]] = []
        for case in _QUALITY_CASES:
            case_results.append(await _run_case(case, retriever, strict_mode=strict_mode))

        required = [result for result in case_results if not result["informational"]]
        informational = [result for result in case_results if result["informational"]]

        required_failures = [result for result in required if result["status"] == "FAIL"]
        required_avg = round(_mean([result["score"] for result in required]), 2)
        info_avg = round(_mean([result["score"] for result in informational]), 2)

        print("\n" + "=" * 88)
        print("  RETRIEVAL QUALITY SUMMARY (default corpus, live local index)")
        print("=" * 88)
        print(
            "  "
            f"schema=v{_SCHEMA_VERSION}  matrix={_MATRIX_VERSION}  "
            f"server={_SERVER_VERSION}  refresh={live_quality_context['metadata'].get('last_refresh_at', 'unknown')}"
        )
        print(
            "  "
            f"docs_hash={(live_quality_context['docs_content_hash'] or 'unknown')[:12]}  "
            f"extra_repos={len(live_quality_context['extra_repos'])}  "
            f"strict_mode={'yes' if strict_mode else 'no'}"
        )
        if live_quality_context["extra_repos"]:
            print(
                "  "
                "warning: extra repos detected; thresholds are informational only for this run."
            )
        print("-" * 88)
        print(f"  {'case':<36} {'tool':<18} {'score':>5}  {'status':<5}  top result")
        print("-" * 88)
        for result in case_results:
            top = result["top_results"][0] if result["top_results"] else "no hits"
            print(
                f"  {result['name']:<36} {result['tool']:<18} "
                f"{result['score']:>5.2f}  {result['status']:<5}  {top}"
            )
        print("-" * 88)
        print(
            "  "
            f"required_avg={required_avg:.2f}  informational_avg={info_avg:.2f}  "
            f"required_failures={len(required_failures)}"
        )
        print("=" * 88)

        report = {
            "schema_version": _SCHEMA_VERSION,
            "matrix_version": _MATRIX_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "server_version": _SERVER_VERSION,
            "index_path": str(live_quality_context["store"].data_dir),
            "last_refresh_at": live_quality_context["metadata"].get("last_refresh_at"),
            "last_refresh_duration_seconds": live_quality_context["metadata"].get(
                "last_refresh_duration_seconds"
            ),
            "docs_content_hash": live_quality_context["docs_content_hash"],
            "total_records": live_quality_context["stats"]["total"],
            "counts_by_type": live_quality_context["stats"]["counts_by_type"],
            "repo_counts": live_quality_context["repo_counts"],
            "repo_shas": live_quality_context["repo_shas"],
            "default_corpus_expected_repos": list(_DEFAULT_REPOS),
            "extra_repos": live_quality_context["extra_repos"],
            "strict_mode": strict_mode,
            "cases": case_results,
            "required_average_score": required_avg,
            "informational_average_score": info_avg,
        }
        _write_report(report)

        assert not strict_mode or not required_failures, (
            "Retrieval quality benchmark failures: "
            + ", ".join(result["name"] for result in required_failures)
        )
