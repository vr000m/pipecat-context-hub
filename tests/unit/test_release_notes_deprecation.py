"""Unit tests for release notes deprecation parsing."""

from __future__ import annotations

from unittest.mock import patch

from pipecat_context_hub.services.ingest.deprecation_map import (
    DeprecationEntry,
    DeprecationMap,
    _extract_module_paths,
    _extract_replacement,
    _parse_release_body,
    build_deprecation_map_from_releases,
)


class TestExtractModulePaths:
    """Test _extract_module_paths from backtick-wrapped text."""

    def test_single_path(self) -> None:
        text = "Deprecated `pipecat.services.grok.llm` module."
        assert _extract_module_paths(text) == ["pipecat.services.grok.llm"]

    def test_multiple_paths(self) -> None:
        text = (
            "`pipecat.services.grok.llm` and `pipecat.services.grok.realtime.llm` "
            "are deprecated. Use `pipecat.services.xai.llm` instead."
        )
        paths = _extract_module_paths(text)
        assert "pipecat.services.grok.llm" in paths
        assert "pipecat.services.grok.realtime.llm" in paths
        assert "pipecat.services.xai.llm" in paths

    def test_no_paths(self) -> None:
        text = "Some text without any module paths."
        assert _extract_module_paths(text) == []

    def test_non_pipecat_paths_ignored(self) -> None:
        text = "`os.path.join` and `pipecat.services.google.llm`"
        assert _extract_module_paths(text) == ["pipecat.services.google.llm"]

    def test_deduplication(self) -> None:
        text = "`pipecat.services.grok.llm` is old, use `pipecat.services.grok.llm` elsewhere"
        assert _extract_module_paths(text) == ["pipecat.services.grok.llm"]


class TestExtractReplacement:
    """Test _extract_replacement from deprecation text."""

    def test_finds_replacement(self) -> None:
        text = (
            "`pipecat.services.grok.llm` is deprecated. "
            "Use `pipecat.services.xai.llm` instead."
        )
        deprecated = ["pipecat.services.grok.llm"]
        assert _extract_replacement(text, deprecated) == "pipecat.services.xai.llm"

    def test_multiple_replacements(self) -> None:
        text = (
            "`pipecat.services.google.llm_vertex` and `pipecat.services.google.llm_openai` "
            "are deprecated. Use `pipecat.services.google.vertex.llm` and "
            "`pipecat.services.google.openai.llm` instead."
        )
        deprecated = [
            "pipecat.services.google.llm_vertex",
            "pipecat.services.google.llm_openai",
        ]
        replacement = _extract_replacement(text, deprecated)
        assert replacement is not None
        assert "pipecat.services.google.vertex.llm" in replacement
        assert "pipecat.services.google.openai.llm" in replacement

    def test_no_replacement(self) -> None:
        text = "Removed `PlayHTTTSService`. PlayHT has been shut down."
        assert _extract_replacement(text, ["PlayHTTTSService"]) is None


class TestParseReleaseBody:
    """Test _parse_release_body for full release note parsing."""

    def test_deprecated_module_path(self) -> None:
        body = (
            "### Deprecated\n"
            "- `pipecat.services.grok.llm`, `pipecat.services.grok.realtime.llm`, and\n"
            "  `pipecat.services.grok.realtime.events` are deprecated. The old import paths\n"
            "  still work but emit a `DeprecationWarning`; use `pipecat.services.xai.llm`,\n"
            "  `pipecat.services.xai.realtime.llm`, and\n"
            "  `pipecat.services.xai.realtime.events` instead.\n"
            "  (PR [#4142](https://github.com/pipecat-ai/pipecat/pull/4142))\n"
        )
        entries = _parse_release_body("0.0.108", body)
        # Should have 3 deprecated entries (grok.llm, grok.realtime.llm, grok.realtime.events)
        deprecated = [e for e in entries if e.deprecated_in == "0.0.108"]
        assert len(deprecated) >= 3
        paths = {e.old_path for e in deprecated}
        assert "pipecat.services.grok.llm" in paths
        assert "pipecat.services.grok.realtime.llm" in paths
        assert "pipecat.services.grok.realtime.events" in paths
        # Each should have replacement
        for e in deprecated:
            if "grok.llm" in e.old_path and "realtime" not in e.old_path:
                assert e.new_path is not None
                assert "xai" in e.new_path

    def test_removed_section(self) -> None:
        body = (
            "### Removed\n"
            "- Removed `SambaNovaSTTService`. SambaNova no longer offers speech-to-text.\n"
        )
        entries = _parse_release_body("0.0.108", body)
        assert len(entries) >= 1
        removed = [e for e in entries if e.removed_in == "0.0.108"]
        assert len(removed) >= 1
        assert removed[0].old_path == "SambaNovaSTTService"

    def test_class_name_extraction(self) -> None:
        body = (
            "### Deprecated\n"
            "- Deprecated `FalSmartTurnAnalyzer` and `LocalSmartTurnAnalyzer`. "
            "Use `LocalSmartTurnAnalyzerV3` instead.\n"
        )
        entries = _parse_release_body("0.0.98", body)
        names = {e.old_path for e in entries}
        assert "FalSmartTurnAnalyzer" in names
        assert "LocalSmartTurnAnalyzer" in names

    def test_dotted_symbol_extraction(self) -> None:
        """Dotted identifiers like SimliVideoService.InputParams are stored as real keys."""
        body = (
            "### Deprecated\n"
            "- Deprecated `SimliVideoService.InputParams`. Use the new params API.\n"
        )
        entries = _parse_release_body("0.0.110", body)
        names = {e.old_path for e in entries}
        assert "SimliVideoService.InputParams" in names

    def test_dotted_symbol_queryable(self) -> None:
        """Dotted symbol entries can be found via DeprecationMap.check()."""
        dm = DeprecationMap(entries={
            "SimliVideoService.InputParams": DeprecationEntry(
                old_path="SimliVideoService.InputParams",
                deprecated_in="0.0.110",
            ),
        })
        result = dm.check("SimliVideoService.InputParams")
        assert result is not None
        assert result.deprecated_in == "0.0.110"

    def test_no_deprecated_sections(self) -> None:
        body = "### Added\n- New feature.\n### Fixed\n- Bugfix.\n"
        entries = _parse_release_body("0.0.107", body)
        assert entries == []

    def test_mixed_sections(self) -> None:
        body = (
            "### Added\n"
            "- New feature.\n"
            "### Deprecated\n"
            "- `pipecat.turns.mute` is deprecated. Use `pipecat.turns.user_mute` instead.\n"
            "### Fixed\n"
            "- Bugfix.\n"
            "### Removed\n"
            "- Removed the deprecated VLLM-based Ultravox STT service.\n"
        )
        entries = _parse_release_body("0.0.99", body)
        deprecated = [e for e in entries if e.deprecated_in]
        removed = [e for e in entries if e.removed_in]
        assert len(deprecated) >= 1
        assert len(removed) >= 1
        assert deprecated[0].old_path == "pipecat.turns.mute"
        assert deprecated[0].new_path is not None
        assert "user_mute" in deprecated[0].new_path

    def test_multiline_item(self) -> None:
        """Items that span multiple lines are joined."""
        body = (
            "### Deprecated\n"
            "- Deprecated `pipecat.services.google.llm_vertex`,\n"
            "  `pipecat.services.google.llm_openai`, and\n"
            "  `pipecat.services.google.gemini_live.llm_vertex` modules.\n"
            "  Use `pipecat.services.google.vertex.llm` instead.\n"
            "  (PR [#3980](https://github.com/pipecat-ai/pipecat/pull/3980))\n"
        )
        entries = _parse_release_body("0.0.105", body)
        paths = {e.old_path for e in entries}
        assert "pipecat.services.google.llm_vertex" in paths
        assert "pipecat.services.google.llm_openai" in paths
        assert "pipecat.services.google.gemini_live.llm_vertex" in paths

    def test_parameter_deprecation(self) -> None:
        """Parameter-level deprecations without module paths use class name."""
        body = (
            "### Deprecated\n"
            "- `SimliVideoService.InputParams` is deprecated. "
            "Use the direct constructor parameters instead.\n"
        )
        entries = _parse_release_body("0.0.106", body)
        # Should extract the dotted reference
        assert len(entries) >= 1


class TestBuildFromReleases:
    """Test build_deprecation_map_from_releases."""

    def test_populates_from_mock_releases(self) -> None:
        mock_releases = [
            ("0.0.108", (
                "### Deprecated\n"
                "- `pipecat.services.grok.llm` is deprecated. "
                "Use `pipecat.services.xai.llm` instead.\n"
                "### Removed\n"
                "- Removed `SambaNovaSTTService`.\n"
            )),
            ("0.0.106", (
                "### Deprecated\n"
                "- Deprecated `WakeCheckFilter` in favor of "
                "`WakePhraseUserTurnStartStrategy`.\n"
            )),
        ]
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=mock_releases,
        ):
            dm = build_deprecation_map_from_releases("pipecat-ai/pipecat")

        assert len(dm.entries) >= 3
        grok = dm.check("pipecat.services.grok.llm")
        assert grok is not None
        assert grok.deprecated_in == "0.0.108"
        assert grok.new_path is not None
        assert "xai" in grok.new_path

        samba = dm.check("SambaNovaSTTService")
        assert samba is not None
        assert samba.removed_in == "0.0.108"

    def test_does_not_overwrite_existing(self) -> None:
        """Release entries don't overwrite source-derived entries."""
        existing = DeprecationMap(
            entries={
                "pipecat.services.grok": DeprecationEntry(
                    old_path="pipecat.services.grok",
                    new_path="pipecat.services.xai.llm",
                    note="From DeprecatedModuleProxy",
                ),
            }
        )
        mock_releases = [
            ("0.0.108", (
                "### Deprecated\n"
                "- `pipecat.services.grok` is deprecated.\n"
            )),
        ]
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=mock_releases,
        ):
            dm = build_deprecation_map_from_releases(
                "pipecat-ai/pipecat", existing
            )

        # Should keep the original entry's note, not overwrite
        assert dm.entries["pipecat.services.grok"].note == "From DeprecatedModuleProxy"
        # But should merge the missing deprecated_in field
        assert dm.entries["pipecat.services.grok"].deprecated_in == "0.0.108"

    def test_merge_missing_lifecycle_fields(self) -> None:
        """Release data merges deprecated_in/removed_in into existing entries."""
        existing = DeprecationMap(
            entries={
                "pipecat.services.old": DeprecationEntry(
                    old_path="pipecat.services.old",
                    new_path="pipecat.services.new",
                    note="From source",
                ),
            }
        )
        mock_releases = [
            ("0.0.105", (
                "### Deprecated\n"
                "- `pipecat.services.old` is deprecated. Use `pipecat.services.new`.\n"
            )),
            ("0.0.110", (
                "### Removed\n"
                "- `pipecat.services.old` has been removed.\n"
            )),
        ]
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=mock_releases,
        ):
            dm = build_deprecation_map_from_releases(
                "pipecat-ai/pipecat", existing
            )

        entry = dm.entries["pipecat.services.old"]
        assert entry.note == "From source"  # not overwritten
        assert entry.deprecated_in == "0.0.105"  # merged
        assert entry.removed_in == "0.0.110"  # merged

    def test_gh_not_available(self) -> None:
        """Gracefully returns empty when gh CLI is not available."""
        with patch(
            "pipecat_context_hub.services.ingest.deprecation_map._fetch_release_notes",
            return_value=[],
        ):
            dm = build_deprecation_map_from_releases("pipecat-ai/pipecat")
        assert len(dm.entries) == 0


class TestRealReleaseNotes:
    """Smoke tests against real GitHub releases (requires gh CLI)."""

    def test_real_pipecat_releases(self) -> None:
        """Fetch real release notes and verify parsing produces entries."""
        from pipecat_context_hub.services.ingest.deprecation_map import (
            _fetch_release_notes,
        )

        releases = _fetch_release_notes("pipecat-ai/pipecat", limit=5)
        if not releases:
            return  # gh not available or not authenticated

        dm = build_deprecation_map_from_releases(
            "pipecat-ai/pipecat", limit=5
        )
        # v0.0.104-108 all have deprecations, so we should get entries
        assert len(dm.entries) >= 3, (
            f"Expected >= 3 entries from real releases, got {len(dm.entries)}"
        )

        # Verify a known deprecation from v0.0.108
        grok = dm.check("pipecat.services.grok.llm")
        if grok:
            assert grok.deprecated_in == "0.0.108"
            assert grok.new_path is not None
            assert "xai" in grok.new_path
