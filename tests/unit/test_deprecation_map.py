"""Unit tests for the deprecation map builder and checker."""

from __future__ import annotations

import json
from pathlib import Path

from pipecat_context_hub.services.ingest.deprecation_map import (
    DeprecationEntry,
    DeprecationMap,
    _expand_bracket_module,
    build_deprecation_map_from_changelog,
    build_deprecation_map_from_source,
)


class TestExpandBracketModule:
    """Test bracket-expansion of module path strings."""

    def test_no_brackets(self) -> None:
        assert _expand_bracket_module("lmnt.tts") == ["lmnt.tts"]

    def test_suffix_brackets(self) -> None:
        result = _expand_bracket_module("cartesia.[stt,tts]")
        assert result == ["cartesia.stt", "cartesia.tts"]

    def test_prefix_brackets(self) -> None:
        result = _expand_bracket_module("[ai_service,image_service,llm_service]")
        assert result == ["ai_service", "image_service", "llm_service"]

    def test_brackets_with_spaces(self) -> None:
        result = _expand_bracket_module("azure.[llm, stt, tts]")
        assert result == ["azure.llm", "azure.stt", "azure.tts"]

    def test_many_items(self) -> None:
        result = _expand_bracket_module(
            "[ai_service,image_service,llm_service,stt_service,tts_service,vision_service]"
        )
        assert len(result) == 6
        assert "ai_service" in result
        assert "vision_service" in result


class TestDeprecationMapCheck:
    """Test the fuzzy matching in DeprecationMap.check()."""

    def _make_map(self) -> DeprecationMap:
        return DeprecationMap(
            entries={
                "pipecat.services.grok": DeprecationEntry(
                    old_path="pipecat.services.grok",
                    new_path="pipecat.services.xai.llm",
                ),
                "pipecat.services.cartesia": DeprecationEntry(
                    old_path="pipecat.services.cartesia",
                    new_path="pipecat.services.cartesia.stt, pipecat.services.cartesia.tts",
                ),
            }
        )

    def test_exact_match(self) -> None:
        dm = self._make_map()
        entry = dm.check("pipecat.services.grok")
        assert entry is not None
        assert entry.new_path == "pipecat.services.xai.llm"

    def test_prefix_match_child(self) -> None:
        """'pipecat.services.grok.llm' should match 'pipecat.services.grok'."""
        dm = self._make_map()
        entry = dm.check("pipecat.services.grok.llm")
        assert entry is not None
        assert entry.old_path == "pipecat.services.grok"

    def test_prefix_match_parent(self) -> None:
        """'pipecat.services' should match 'pipecat.services.grok' (reverse prefix)."""
        dm = self._make_map()
        entry = dm.check("pipecat.services")
        assert entry is not None

    def test_no_match(self) -> None:
        dm = self._make_map()
        assert dm.check("pipecat.transports.daily") is None

    def test_empty_map(self) -> None:
        dm = DeprecationMap()
        assert dm.check("anything") is None


class TestDeprecationMapSerialization:
    """Test save/load round-trip."""

    def test_round_trip(self, tmp_path: Path) -> None:
        original = DeprecationMap(
            entries={
                "pipecat.services.grok": DeprecationEntry(
                    old_path="pipecat.services.grok",
                    new_path="pipecat.services.xai.llm",
                    deprecated_in="0.0.100",
                    note="Use xai.llm instead",
                ),
            },
            pipecat_commit_sha="abc123",
        )
        path = tmp_path / "deprecation_map.json"
        original.save(path)

        loaded = DeprecationMap.load(path)
        assert loaded.pipecat_commit_sha == "abc123"
        assert "pipecat.services.grok" in loaded.entries
        entry = loaded.entries["pipecat.services.grok"]
        assert entry.new_path == "pipecat.services.xai.llm"
        assert entry.deprecated_in == "0.0.100"
        assert entry.note == "Use xai.llm instead"

    def test_load_missing_file(self, tmp_path: Path) -> None:
        loaded = DeprecationMap.load(tmp_path / "nonexistent.json")
        assert loaded.entries == {}

    def test_to_dict_from_dict(self) -> None:
        dm = DeprecationMap(
            entries={
                "key": DeprecationEntry(
                    old_path="key",
                    new_path="new_key",
                    removed_in="0.0.110",
                ),
            },
            pipecat_commit_sha="def456",
        )
        data = dm.to_dict()
        restored = DeprecationMap.from_dict(data)
        assert restored.pipecat_commit_sha == "def456"
        assert "key" in restored.entries
        assert restored.entries["key"].removed_in == "0.0.110"


class TestBuildFromSource:
    """Test parsing DeprecatedModuleProxy from pipecat source files."""

    def _make_pipecat_source(self, tmp_path: Path) -> Path:
        """Create a minimal pipecat source tree with deprecation proxies."""
        services = tmp_path / "src" / "pipecat" / "services"

        # grok/__init__.py — simple redirect
        grok = services / "grok"
        grok.mkdir(parents=True)
        (grok / "__init__.py").write_text(
            'import sys\n'
            'from pipecat.services import DeprecatedModuleProxy\n'
            'sys.modules[__name__] = DeprecatedModuleProxy(globals(), "grok", "xai.llm")\n'
        )

        # cartesia/__init__.py — bracket expansion
        cartesia = services / "cartesia"
        cartesia.mkdir(parents=True)
        (cartesia / "__init__.py").write_text(
            'import sys\n'
            'from pipecat.services import DeprecatedModuleProxy\n'
            'sys.modules[__name__] = DeprecatedModuleProxy(globals(), "cartesia", "cartesia.[stt,tts]")\n'
        )

        # ai_services.py — bracket-only expansion (file, not __init__)
        (services / "ai_services.py").write_text(
            'import sys\n'
            'from pipecat.services import DeprecatedModuleProxy\n'
            'sys.modules[__name__] = DeprecatedModuleProxy(\n'
            '    globals(),\n'
            '    "ai_services",\n'
            '    "[ai_service,image_service,llm_service]",\n'
            ')\n'
        )

        # __init__.py for services (define DeprecatedModuleProxy class)
        (services / "__init__.py").write_text(
            'class DeprecatedModuleProxy:\n'
            '    pass\n'
        )

        return tmp_path

    def test_simple_redirect(self, tmp_path: Path) -> None:
        repo = self._make_pipecat_source(tmp_path)
        dm = build_deprecation_map_from_source(repo, commit_sha="test123")
        entry = dm.check("pipecat.services.grok")
        assert entry is not None
        assert entry.new_path == "pipecat.services.xai.llm"
        assert dm.pipecat_commit_sha == "test123"

    def test_bracket_expansion(self, tmp_path: Path) -> None:
        repo = self._make_pipecat_source(tmp_path)
        dm = build_deprecation_map_from_source(repo)
        entry = dm.check("pipecat.services.cartesia")
        assert entry is not None
        assert "pipecat.services.cartesia.stt" in (entry.new_path or "")
        assert "pipecat.services.cartesia.tts" in (entry.new_path or "")

    def test_file_level_deprecation(self, tmp_path: Path) -> None:
        repo = self._make_pipecat_source(tmp_path)
        dm = build_deprecation_map_from_source(repo)
        entry = dm.check("pipecat.services.ai_services")
        assert entry is not None
        assert "ai_service" in (entry.new_path or "")

    def test_missing_source(self, tmp_path: Path) -> None:
        dm = build_deprecation_map_from_source(tmp_path / "nonexistent")
        assert len(dm.entries) == 0

    def test_total_entries(self, tmp_path: Path) -> None:
        repo = self._make_pipecat_source(tmp_path)
        dm = build_deprecation_map_from_source(repo)
        # Should have 3 entries: grok, cartesia, ai_services
        assert len(dm.entries) == 3


class TestBuildFromChangelog:
    """Test CHANGELOG parsing for deprecation entries."""

    def test_deprecated_section(self, tmp_path: Path) -> None:
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "## [0.0.100] - 2024-01-01\n"
            "### Deprecated\n"
            "- `pipecat.services.grok` is deprecated, use `pipecat.services.xai`.\n"
            "### Fixed\n"
            "- Some bugfix.\n"
        )
        dm = build_deprecation_map_from_changelog(changelog)
        # CHANGELOG entries go to changelog_notes, not entries
        matching = [n for n in dm.changelog_notes if n.deprecated_in == "0.0.100"]
        assert len(matching) == 1
        assert "grok" in matching[0].note

    def test_removed_section(self, tmp_path: Path) -> None:
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "## [0.0.110] - 2024-06-01\n"
            "### Removed\n"
            "- Removed the old `pipecat.services.lmnt` module.\n"
        )
        dm = build_deprecation_map_from_changelog(changelog)
        matching = [n for n in dm.changelog_notes if n.removed_in == "0.0.110"]
        assert len(matching) == 1

    def test_supplements_existing_map(self, tmp_path: Path) -> None:
        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text(
            "## [0.0.100] - 2024-01-01\n"
            "### Deprecated\n"
            "- Some deprecation.\n"
        )
        existing = DeprecationMap(
            entries={
                "pipecat.services.grok": DeprecationEntry(
                    old_path="pipecat.services.grok",
                    new_path="pipecat.services.xai.llm",
                ),
            }
        )
        result = build_deprecation_map_from_changelog(changelog, existing)
        # entries preserved, changelog_notes added separately
        assert "pipecat.services.grok" in result.entries
        assert len(result.changelog_notes) >= 1

    def test_missing_changelog(self, tmp_path: Path) -> None:
        dm = build_deprecation_map_from_changelog(tmp_path / "CHANGELOG.md")
        assert len(dm.changelog_notes) == 0


class TestCheckDeprecationHandler:
    """Test the MCP tool handler for check_deprecation."""

    async def test_deprecated_symbol(self) -> None:
        from pipecat_context_hub.server.tools.check_deprecation import (
            handle_check_deprecation,
        )

        dm = DeprecationMap(
            entries={
                "pipecat.services.grok": DeprecationEntry(
                    old_path="pipecat.services.grok",
                    new_path="pipecat.services.xai.llm",
                    deprecated_in="0.0.100",
                ),
            }
        )
        result_json = await handle_check_deprecation(
            {"symbol": "pipecat.services.grok.llm"}, dm
        )
        result = json.loads(result_json)
        assert result["deprecated"] is True
        assert result["replacement"] == "pipecat.services.xai.llm"

    async def test_not_deprecated(self) -> None:
        from pipecat_context_hub.server.tools.check_deprecation import (
            handle_check_deprecation,
        )

        dm = DeprecationMap()
        result_json = await handle_check_deprecation(
            {"symbol": "DailyTransport"}, dm
        )
        result = json.loads(result_json)
        assert result["deprecated"] is False

    async def test_no_map_available(self) -> None:
        from pipecat_context_hub.server.tools.check_deprecation import (
            handle_check_deprecation,
        )

        result_json = await handle_check_deprecation(
            {"symbol": "pipecat.services.grok"}, None
        )
        result = json.loads(result_json)
        assert result["deprecated"] is False
        assert "not available" in (result.get("note") or "")


class TestBuildFromRealSource:
    """Smoke test against the actual cloned pipecat repo (if available)."""

    def test_real_pipecat_source(self) -> None:
        """If the pipecat repo is cloned locally, verify parsing doesn't crash.

        Note: pipecat may have removed DeprecatedModuleProxy redirects in
        recent versions (e.g., PR #4240), so we don't assert a minimum count.
        """
        pipecat_repo = Path.home() / ".pipecat-context-hub" / "repos" / "pipecat-ai_pipecat"
        if not (pipecat_repo / "src" / "pipecat" / "services").is_dir():
            return  # Skip if not available

        dm = build_deprecation_map_from_source(pipecat_repo)
        # Just verify it doesn't crash and returns a valid map
        assert isinstance(dm, DeprecationMap)

    def test_real_changelog(self) -> None:
        """If the pipecat CHANGELOG exists, verify parsing extracts entries."""
        changelog = (
            Path.home()
            / ".pipecat-context-hub"
            / "repos"
            / "pipecat-ai_pipecat"
            / "CHANGELOG.md"
        )
        if not changelog.is_file():
            return  # Skip if not available

        dm = build_deprecation_map_from_changelog(changelog)
        # CHANGELOG should have at least some Deprecated/Removed notes
        assert len(dm.changelog_notes) >= 1, (
            f"Expected >= 1 CHANGELOG note, got {len(dm.changelog_notes)}"
        )
