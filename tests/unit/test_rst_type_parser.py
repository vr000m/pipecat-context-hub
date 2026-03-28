"""Unit tests for RST type definition parser."""

from __future__ import annotations

from pathlib import Path

from pipecat_context_hub.services.ingest.rst_type_parser import (
    RstField,
    _strip_rst_markup,
    parse_rst_types,
)


class TestStripRstMarkup:
    def test_external_link(self):
        text = "`MediaTrackConstraints <https://example.com>`_"
        assert _strip_rst_markup(text) == "MediaTrackConstraints"

    def test_internal_ref(self):
        assert _strip_rst_markup("`CustomTrack`_") == "CustomTrack"

    def test_emphasis(self):
        assert _strip_rst_markup("*None*") == "None"

    def test_plain_text(self):
        assert _strip_rst_markup("string") == "string"

    def test_mixed(self):
        text = "bool (see `Deepgram docs <https://example.com>`_)"
        result = _strip_rst_markup(text)
        assert "Deepgram docs" in result
        assert "<https" not in result


class TestParseDictType:
    RST = """\
.. _DialoutSendDtmfSettings:

DialoutSendDtmfSettings
-----------------------------------

.. list-table::
   :widths: 25 75
   :header-rows: 1

   * - Key
     - Value
   * - "sessionId"
     - string
   * - "tones"
     - string
   * - "method"
     - "sip-info" | "telephone-event" | "auto"
   * - "digitDurationMs"
     - number

.. _NextType:

NextType
-----------------------------------

A string.
"""

    def test_parses_dict_fields(self, tmp_path: Path):
        rst_file = tmp_path / "types.rst"
        rst_file.write_text(self.RST)
        types = parse_rst_types(rst_file)

        dtmf = next(t for t in types if t.name == "DialoutSendDtmfSettings")
        assert dtmf.kind == "dict"
        assert len(dtmf.fields) == 4
        assert dtmf.fields[0] == RstField(key="sessionId", value_type="string")
        assert dtmf.fields[1] == RstField(key="tones", value_type="string")
        assert dtmf.fields[2].key == "method"
        assert "sip-info" in dtmf.fields[2].value_type
        assert dtmf.fields[3] == RstField(key="digitDurationMs", value_type="number")

    def test_renders_content(self, tmp_path: Path):
        rst_file = tmp_path / "types.rst"
        rst_file.write_text(self.RST)
        types = parse_rst_types(rst_file)

        dtmf = next(t for t in types if t.name == "DialoutSendDtmfSettings")
        content = dtmf.render_content("daily")
        assert "# Type: DialoutSendDtmfSettings" in content
        assert "Module: daily" in content
        assert '"sessionId": string' in content
        assert '"digitDurationMs": number' in content


class TestParseEnumType:
    RST = """\
.. _CallState:

CallState
-----------------------------------

"initialized" | "joining" | "joined" | "leaving" | "left"

.. _NextType:

NextType
-----------------------------------

A string.
"""

    def test_parses_enum(self, tmp_path: Path):
        rst_file = tmp_path / "types.rst"
        rst_file.write_text(self.RST)
        types = parse_rst_types(rst_file)

        cs = next(t for t in types if t.name == "CallState")
        assert cs.kind == "enum"
        assert "initialized" in cs.description
        assert "left" in cs.description

    def test_renders_enum(self, tmp_path: Path):
        rst_file = tmp_path / "types.rst"
        rst_file.write_text(self.RST)
        types = parse_rst_types(rst_file)

        cs = next(t for t in types if t.name == "CallState")
        content = cs.render_content("daily")
        assert "Enum:" in content


class TestParseAliasType:
    RST = """\
.. _CallClientError:

CallClientError
-----------------------------------

A string with an error message or *None*.

.. _NextType:

NextType
-----------------------------------

"a" | "b"
"""

    def test_parses_alias(self, tmp_path: Path):
        rst_file = tmp_path / "types.rst"
        rst_file.write_text(self.RST)
        types = parse_rst_types(rst_file)

        err = next(t for t in types if t.name == "CallClientError")
        assert err.kind == "alias"
        assert err.description == ""  # alias prose is never stored


    def test_alias_never_renders_prose(self, tmp_path: Path):
        """Alias content must never include untrusted RST prose."""
        rst_file = tmp_path / "types.rst"
        rst_file.write_text(self.RST)
        types = parse_rst_types(rst_file)

        err = next(t for t in types if t.name == "CallClientError")
        content = err.render_content("daily")
        assert "see source" in content.lower()
        assert "error message" not in content

    def test_prose_alias_does_not_render_raw_text(self, tmp_path: Path):
        """Regression: free-form prose from RST must not appear in snippet content."""
        rst = """\
.. _StreamingLayout:

StreamingLayout
-----------------------------------

For more details see the layout object.

.. _NextType:

NextType
-----------------------------------

"a" | "b"
"""
        rst_file = tmp_path / "types.rst"
        rst_file.write_text(rst)
        types = parse_rst_types(rst_file)

        sl = next(t for t in types if t.name == "StreamingLayout")
        assert sl.kind == "alias"
        content = sl.render_content("daily")
        # Must NOT contain the raw prose text
        assert "For more details" not in content
        assert "layout object" not in content
        # Should have a safe fallback
        assert "see source" in content.lower() or "alias" in content.lower()


class TestParseOrPattern:
    RST = """\
.. _AudioInputSettings:

AudioInputSettings
-----------------------------------

.. list-table::
   :widths: 25 75
   :header-rows: 1

   * - Key
     - Value
   * - "deviceId"
     - string
   * - "customConstraints"
     - `MediaTrackConstraints <https://example.com>`_

or

.. list-table::
   :widths: 25 75
   :header-rows: 1

   * - Key
     - Value
   * - "customTrack"
     - `CustomTrack`_

.. _NextType:

NextType
-----------------------------------

"a" | "b"
"""

    def test_parses_or_alternatives(self, tmp_path: Path):
        rst_file = tmp_path / "types.rst"
        rst_file.write_text(self.RST)
        types = parse_rst_types(rst_file)

        ais = next(t for t in types if t.name == "AudioInputSettings")
        assert ais.kind == "dict_or"
        assert len(ais.alternatives) == 2
        assert ais.alternatives[0][0].key == "deviceId"
        assert ais.alternatives[1][0].key == "customTrack"

    def test_renders_or_content(self, tmp_path: Path):
        rst_file = tmp_path / "types.rst"
        rst_file.write_text(self.RST)
        types = parse_rst_types(rst_file)

        ais = next(t for t in types if t.name == "AudioInputSettings")
        content = ais.render_content("daily")
        assert "Or:" in content
        assert '"deviceId"' in content
        assert '"customTrack"' in content


class TestParseRstRefs:
    RST = """\
.. _CanReceivePermission:

CanReceivePermission
-----------------------------------

.. list-table::
   :widths: 25 75
   :header-rows: 1

   * - Key
     - Value
   * - "base"
     - bool | `CanReceiveMediaPermission`_
   * - "byUserId"
     - Mapping[string, bool | `CanReceiveMediaPermission`_]

.. _NextType:

NextType
-----------------------------------

"a"
"""

    def test_extracts_refs(self, tmp_path: Path):
        rst_file = tmp_path / "types.rst"
        rst_file.write_text(self.RST)
        types = parse_rst_types(rst_file)

        crp = next(t for t in types if t.name == "CanReceivePermission")
        assert "CanReceiveMediaPermission" in crp.rst_refs


class TestParseLiveFile:
    """Test against the actual daily-python types.rst if available."""

    def test_parses_real_file(self):
        rst_path = Path.home() / ".pipecat-context-hub/repos/daily-co_daily-python/docs/src/types.rst"
        if not rst_path.is_file():
            import pytest
            pytest.skip("daily-python not cloned locally")

        types = parse_rst_types(rst_path)

        # Should find many types
        assert len(types) >= 50

        # Check known types exist
        names = {t.name for t in types}
        assert "DialoutSendDtmfSettings" in names
        assert "CallState" in names
        assert "CallClientError" in names
        assert "AudioInputSettings" in names

        # Check DialoutSendDtmfSettings fields
        dtmf = next(t for t in types if t.name == "DialoutSendDtmfSettings")
        assert dtmf.kind == "dict"
        field_keys = [f.key for f in dtmf.fields]
        assert "sessionId" in field_keys
        assert "tones" in field_keys

        # Check AudioInputSettings has "or" pattern
        ais = next(t for t in types if t.name == "AudioInputSettings")
        assert ais.kind == "dict_or"
        assert len(ais.alternatives) >= 2
