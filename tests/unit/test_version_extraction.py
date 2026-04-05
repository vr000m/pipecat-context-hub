"""Unit tests for pipecat version extraction from dependency files."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipecat_context_hub.services.ingest.github_ingest import (
    _extract_pipecat_version,
    _get_framework_version,
    _parse_pipecat_version_from_package_json,
    _parse_pipecat_version_from_pyproject,
    _parse_pipecat_version_from_requirements,
)


class TestParsePyproject:
    """Test _parse_pipecat_version_from_pyproject."""

    def test_exact_pin(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "my-bot"\ndependencies = ["pipecat-ai==0.0.98"]\n'
        )
        assert _parse_pipecat_version_from_pyproject(pyproject) == "==0.0.98"

    def test_minimum_version(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "my-bot"\ndependencies = ["pipecat-ai>=0.0.105"]\n'
        )
        assert _parse_pipecat_version_from_pyproject(pyproject) == ">=0.0.105"

    def test_range(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "peekaboo"\ndependencies = ["pipecat-ai>=0.0.93,<1"]\n'
        )
        assert _parse_pipecat_version_from_pyproject(pyproject) == "<1,>=0.0.93"

    def test_extras_syntax(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "my-bot"\n'
            'dependencies = ["pipecat-ai[daily,runner]>=0.0.105"]\n'
        )
        assert _parse_pipecat_version_from_pyproject(pyproject) == ">=0.0.105"

    def test_no_version_constraint(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "my-bot"\n'
            'dependencies = ["pipecat-ai[webrtc,daily]"]\n'
        )
        assert _parse_pipecat_version_from_pyproject(pyproject) is None

    def test_no_pipecat_dep(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "unrelated"\ndependencies = ["requests>=2.0"]\n'
        )
        assert _parse_pipecat_version_from_pyproject(pyproject) is None

    def test_empty_dependencies(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "monorepo-root"\ndependencies = []\n'
        )
        assert _parse_pipecat_version_from_pyproject(pyproject) is None

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _parse_pipecat_version_from_pyproject(tmp_path / "nonexistent.toml") is None


class TestParseRequirements:
    """Test _parse_pipecat_version_from_requirements."""

    def test_basic_pin(self, tmp_path: Path) -> None:
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("pipecat-ai[daily]>=0.0.100,<0.1\nfastapi>=0.100\n")
        assert _parse_pipecat_version_from_requirements(reqs) == "<0.1,>=0.0.100"

    def test_simple_requirement(self, tmp_path: Path) -> None:
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("pipecat-ai==0.0.95\n")
        assert _parse_pipecat_version_from_requirements(reqs) == "==0.0.95"

    def test_no_pipecat(self, tmp_path: Path) -> None:
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("fastapi>=0.100\nuvicorn\n")
        assert _parse_pipecat_version_from_requirements(reqs) is None

    def test_comments_and_blanks(self, tmp_path: Path) -> None:
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("# deps\n\npipecat-ai>=0.0.90\n# end\n")
        assert _parse_pipecat_version_from_requirements(reqs) == ">=0.0.90"

    def test_dash_prefixed_lines_skipped(self, tmp_path: Path) -> None:
        """Lines starting with - (e.g. -r, -e) should be skipped."""
        reqs = tmp_path / "requirements.txt"
        reqs.write_text("-r base.txt\npipecat-ai>=0.0.100\n")
        assert _parse_pipecat_version_from_requirements(reqs) == ">=0.0.100"

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _parse_pipecat_version_from_requirements(tmp_path / "nope.txt") is None


class TestParsePackageJson:
    """Test _parse_pipecat_version_from_package_json."""

    def test_caret_range(self, tmp_path: Path) -> None:
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({
            "dependencies": {"@pipecat-ai/client-js": "^1.7.0"}
        }))
        assert _parse_pipecat_version_from_package_json(pkg) == "^1.7.0"

    def test_dev_dependency(self, tmp_path: Path) -> None:
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({
            "devDependencies": {"@pipecat-ai/client-js": "~2.0.0"}
        }))
        assert _parse_pipecat_version_from_package_json(pkg) == "~2.0.0"

    def test_no_pipecat(self, tmp_path: Path) -> None:
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({"dependencies": {"react": "^18.0.0"}}))
        assert _parse_pipecat_version_from_package_json(pkg) is None

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _parse_pipecat_version_from_package_json(tmp_path / "nope.json") is None


class TestExtractPipecatVersion:
    """Test _extract_pipecat_version walk-upward."""

    def test_finds_version_in_example_dir(self, tmp_path: Path) -> None:
        """Version in the example directory itself."""
        repo_root = tmp_path / "repo"
        example = repo_root / "examples" / "my-bot"
        example.mkdir(parents=True)
        (example / "pyproject.toml").write_text(
            '[project]\ndependencies = ["pipecat-ai==0.0.98"]\n'
        )
        assert _extract_pipecat_version(example, repo_root) == "==0.0.98"

    def test_walks_up_to_find_version(self, tmp_path: Path) -> None:
        """Version in a parent directory, not the example dir itself."""
        repo_root = tmp_path / "repo"
        example = repo_root / "examples" / "subdir" / "my-bot"
        example.mkdir(parents=True)
        # Version at examples/ level
        (repo_root / "examples" / "subdir" / "pyproject.toml").write_text(
            '[project]\ndependencies = ["pipecat-ai>=0.0.100"]\n'
        )
        assert _extract_pipecat_version(example, repo_root) == ">=0.0.100"

    def test_monorepo_skips_empty_root(self, tmp_path: Path) -> None:
        """Root pyproject.toml with empty deps is skipped; subdir has pin."""
        repo_root = tmp_path / "repo"
        example = repo_root / "examples" / "my-bot"
        example.mkdir(parents=True)
        # Root has empty deps
        (repo_root / "pyproject.toml").write_text(
            '[project]\nname = "monorepo"\ndependencies = []\n'
        )
        # Example dir has the pin
        (example / "pyproject.toml").write_text(
            '[project]\ndependencies = ["pipecat-ai[daily]==0.0.105"]\n'
        )
        assert _extract_pipecat_version(example, repo_root) == "==0.0.105"

    def test_requirements_txt_fallback(self, tmp_path: Path) -> None:
        """Falls back to requirements.txt when pyproject has no pipecat dep."""
        repo_root = tmp_path / "repo"
        example = repo_root / "my-example"
        example.mkdir(parents=True)
        (example / "requirements.txt").write_text("pipecat-ai[daily]>=0.0.90\n")
        assert _extract_pipecat_version(example, repo_root) == ">=0.0.90"

    def test_package_json_ts(self, tmp_path: Path) -> None:
        """Finds TS version from package.json."""
        repo_root = tmp_path / "repo"
        example = repo_root / "frontend"
        example.mkdir(parents=True)
        (example / "package.json").write_text(json.dumps({
            "dependencies": {"@pipecat-ai/client-js": "^1.7.0"}
        }))
        assert _extract_pipecat_version(example, repo_root) == "^1.7.0"

    def test_no_version_anywhere(self, tmp_path: Path) -> None:
        """Returns None when no dependency file has pipecat-ai."""
        repo_root = tmp_path / "repo"
        example = repo_root / "examples" / "quickstart"
        example.mkdir(parents=True)
        assert _extract_pipecat_version(example, repo_root) is None

    def test_stops_at_repo_root(self, tmp_path: Path) -> None:
        """Doesn't walk above repo_root."""
        repo_root = tmp_path / "repo"
        example = repo_root / "examples" / "bot"
        example.mkdir(parents=True)
        # Put a pyproject above repo root — should not be found
        (tmp_path / "pyproject.toml").write_text(
            '[project]\ndependencies = ["pipecat-ai==0.0.1"]\n'
        )
        assert _extract_pipecat_version(example, repo_root) is None


class TestGetFrameworkVersion:
    """Test _get_framework_version."""

    def test_returns_version_from_tag(self, tmp_path: Path) -> None:
        mock_repo = MagicMock()
        mock_repo.git.describe.return_value = "v0.0.108"
        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.GitRepo",
            return_value=mock_repo,
        ):
            assert _get_framework_version(tmp_path) == "0.0.108"

    def test_strips_v_prefix(self, tmp_path: Path) -> None:
        mock_repo = MagicMock()
        mock_repo.git.describe.return_value = "v1.2.3"
        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.GitRepo",
            return_value=mock_repo,
        ):
            assert _get_framework_version(tmp_path) == "1.2.3"

    def test_no_tags_returns_none(self, tmp_path: Path) -> None:
        mock_repo = MagicMock()
        mock_repo.git.describe.side_effect = Exception("no tags")
        with patch(
            "pipecat_context_hub.services.ingest.github_ingest.GitRepo",
            return_value=mock_repo,
        ):
            assert _get_framework_version(tmp_path) is None


class TestBuildChunkMetadataVersion:
    """Test that _build_chunk_metadata includes pipecat_version_pin."""

    def test_version_included_when_provided(self) -> None:
        from pipecat_context_hub.services.ingest.github_ingest import _build_chunk_metadata

        meta = _build_chunk_metadata(
            repo_slug="test/repo",
            commit_sha="abc123",
            chunk_index=0,
            language="python",
            line_start=1,
            line_end=10,
            taxonomy_entry=None,
            pipecat_version=">=0.0.105",
        )
        assert meta["pipecat_version_pin"] == ">=0.0.105"

    def test_version_omitted_when_none(self) -> None:
        from pipecat_context_hub.services.ingest.github_ingest import _build_chunk_metadata

        meta = _build_chunk_metadata(
            repo_slug="test/repo",
            commit_sha="abc123",
            chunk_index=0,
            language="python",
            line_start=1,
            line_end=10,
            taxonomy_entry=None,
            pipecat_version=None,
        )
        assert "pipecat_version_pin" not in meta


class TestVectorRoundTrip:
    """Test that pipecat_version_pin survives ChromaDB round-trip."""

    def test_version_pin_round_trip(self) -> None:
        from datetime import datetime, timezone

        from pipecat_context_hub.services.index.vector import (
            _metadata_to_record_fields,
            _record_to_metadata,
        )
        from pipecat_context_hub.shared.types import ChunkedRecord

        record = ChunkedRecord(
            chunk_id="test-001",
            content="test content",
            content_type="code",
            source_url="https://example.com",
            path="test.py",
            indexed_at=datetime.now(tz=timezone.utc),
            metadata={"pipecat_version_pin": ">=0.0.105"},
        )

        meta = _record_to_metadata(record)
        assert meta["pipecat_version_pin"] == ">=0.0.105"

        reconstructed = _metadata_to_record_fields("test-001", "test content", meta)
        assert reconstructed.metadata["pipecat_version_pin"] == ">=0.0.105"

    def test_version_pin_absent(self) -> None:
        from datetime import datetime, timezone

        from pipecat_context_hub.services.index.vector import (
            _metadata_to_record_fields,
            _record_to_metadata,
        )
        from pipecat_context_hub.shared.types import ChunkedRecord

        record = ChunkedRecord(
            chunk_id="test-002",
            content="test content",
            content_type="code",
            source_url="https://example.com",
            path="test.py",
            indexed_at=datetime.now(tz=timezone.utc),
            metadata={},
        )

        meta = _record_to_metadata(record)
        assert "pipecat_version_pin" not in meta

        reconstructed = _metadata_to_record_fields("test-002", "test content", meta)
        assert "pipecat_version_pin" not in reconstructed.metadata
