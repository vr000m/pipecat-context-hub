"""Tests for the AST extractor module."""

from __future__ import annotations

import textwrap


from pipecat_context_hub.services.ingest.ast_extractor import (
    ParameterInfo,
    build_signature,
    extract_module_info,
)


# ---------------------------------------------------------------------------
# Fixtures — reusable source snippets
# ---------------------------------------------------------------------------

PIPECAT_SNIPPET = textwrap.dedent('''\
    """Frame types for the Pipecat framework."""

    from dataclasses import dataclass

    @dataclass
    class Frame:
        """Base frame type."""
        name: str | None = None

    class TTSService:
        """Text-to-speech service base class."""

        def __init__(self, *, aggregate_sentences: bool = True, push_stop_frames: bool = False):
            """Initialize TTS service."""
            self._aggregate_sentences = aggregate_sentences
            self._push_stop_frames = push_stop_frames

        async def run_tts(self, text: str) -> None:
            """Run TTS. Override in subclass."""
            raise NotImplementedError
''')


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSimpleClass:
    """Parse a simple class with one method, verify ClassInfo fields."""

    SOURCE = textwrap.dedent("""\
        class Greeter:
            \"\"\"A simple greeter.\"\"\"

            def greet(self, name: str) -> str:
                \"\"\"Say hello.\"\"\"
                return f"Hello, {name}"
    """)

    def test_class_name(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert len(info.classes) == 1
        assert info.classes[0].name == "Greeter"

    def test_class_docstring(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert info.classes[0].docstring == "A simple greeter."

    def test_method_name(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert len(info.classes[0].methods) == 1
        assert info.classes[0].methods[0].name == "greet"

    def test_method_return_type(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert info.classes[0].methods[0].return_type == "str"

    def test_method_docstring(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert info.classes[0].methods[0].docstring == "Say hello."

    def test_method_params(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        params = info.classes[0].methods[0].parameters
        assert len(params) == 2
        assert params[0].name == "self"
        assert params[1].name == "name"
        assert params[1].annotation == "str"

    def test_line_numbers(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        cls = info.classes[0]
        assert cls.line_start == 1
        assert cls.line_end >= 6


class TestDataclassDetection:
    """Parse ``@dataclass`` class, verify ``is_dataclass=True``."""

    SOURCE = textwrap.dedent("""\
        from dataclasses import dataclass

        @dataclass
        class Config:
            host: str = "localhost"
            port: int = 8080
    """)

    def test_is_dataclass(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert len(info.classes) == 1
        assert info.classes[0].is_dataclass is True

    def test_decorator_listed(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert "dataclass" in info.classes[0].decorators


class TestAbstractMethod:
    """Parse ``@abstractmethod``, verify ``is_abstract=True``."""

    SOURCE = textwrap.dedent("""\
        from abc import ABC, abstractmethod

        class Base(ABC):
            @abstractmethod
            def process(self) -> None:
                ...
    """)

    def test_is_abstract(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        method = info.classes[0].methods[0]
        assert method.is_abstract is True

    def test_decorator_listed(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        method = info.classes[0].methods[0]
        assert "abstractmethod" in method.decorators


class TestTypedParamsWithDefaults:
    """Parse function with typed params and defaults."""

    SOURCE = textwrap.dedent("""\
        def foo(x: int = 5, y: str = "hello") -> bool:
            return True
    """)

    def test_param_count(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        assert len(func.parameters) == 2

    def test_param_annotations(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        params = info.functions[0].parameters
        assert params[0].annotation == "int"
        assert params[1].annotation == "str"

    def test_param_defaults(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        params = info.functions[0].parameters
        assert params[0].default == "5"
        assert params[1].default == "'hello'"

    def test_return_type(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert info.functions[0].return_type == "bool"


class TestModuleDocstring:
    """Parse module with docstring, verify ModuleInfo.docstring."""

    SOURCE = textwrap.dedent('''\
        """This is the module docstring."""

        x = 1
    ''')

    def test_docstring(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert info.docstring == "This is the module docstring."


class TestAllExports:
    """Parse module with ``__all__`` list."""

    SOURCE = textwrap.dedent("""\
        __all__ = ["Foo", "Bar"]

        class Foo:
            pass

        class Bar:
            pass
    """)

    def test_all_exports(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert info.all_exports == ["Foo", "Bar"]


class TestAsyncFunction:
    """Parse ``async def`` function."""

    SOURCE = textwrap.dedent("""\
        async def fetch_data(url: str) -> bytes:
            \"\"\"Fetch data from a URL.\"\"\"
            pass
    """)

    def test_is_extracted(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert len(info.functions) == 1
        assert info.functions[0].name == "fetch_data"

    def test_params(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        params = info.functions[0].parameters
        assert len(params) == 1
        assert params[0].name == "url"
        assert params[0].annotation == "str"

    def test_return_type(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert info.functions[0].return_type == "bytes"

    def test_docstring(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert info.functions[0].docstring == "Fetch data from a URL."


class TestClassWithBases:
    """Parse ``class Foo(Bar, Baz)``, verify base_classes."""

    SOURCE = textwrap.dedent("""\
        class Foo(Bar, Baz):
            pass
    """)

    def test_base_classes(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert info.classes[0].base_classes == ["Bar", "Baz"]


class TestBuildSignature:
    """Test signature builder with various parameter combinations."""

    def test_no_params_no_return(self):
        sig = build_signature("foo", [], None)
        assert sig == "()"

    def test_self_only(self):
        sig = build_signature("bar", [ParameterInfo(name="self")], None)
        assert sig == "(self)"

    def test_typed_params_with_defaults(self):
        params = [
            ParameterInfo(name="self"),
            ParameterInfo(name="x", annotation="int", default="5"),
            ParameterInfo(name="y", annotation="str"),
        ]
        sig = build_signature("baz", params, "bool")
        assert sig == "(self, x: int = 5, y: str) -> bool"

    def test_return_type_only(self):
        sig = build_signature("qux", [], "None")
        assert sig == "() -> None"

    def test_kwargs(self):
        params = [
            ParameterInfo(name="**kwargs", annotation="Any"),
        ]
        sig = build_signature("func", params, None)
        assert sig == "(**kwargs: Any)"

    def test_posonly_separator(self):
        params = [
            ParameterInfo(name="a", annotation="int"),
            ParameterInfo(name="/"),
            ParameterInfo(name="b", annotation="str"),
        ]
        sig = build_signature("func", params, None)
        assert sig == "(a: int, /, b: str)"

    def test_posonly_and_kwonly_separators(self):
        params = [
            ParameterInfo(name="a", annotation="int"),
            ParameterInfo(name="/"),
            ParameterInfo(name="b"),
            ParameterInfo(name="*"),
            ParameterInfo(name="c", annotation="bool", default="True"),
        ]
        sig = build_signature("func", params, "None")
        assert sig == "(a: int, /, b, *, c: bool = True) -> None"


class TestEmptyModule:
    """Parse empty string, verify empty ModuleInfo."""

    def test_empty_source(self):
        info = extract_module_info("", "empty")
        assert info.module_path == "empty"
        assert info.docstring is None
        assert info.classes == []
        assert info.functions == []
        assert info.all_exports == []
        assert info.imports == []

    def test_whitespace_only(self):
        info = extract_module_info("   \n  \n  ", "ws")
        assert info.module_path == "ws"
        assert info.classes == []


class TestMethodSourceExtraction:
    """Verify method source is correctly extracted from lines."""

    SOURCE = textwrap.dedent("""\
        class MyClass:
            def my_method(self) -> None:
                x = 1
                y = 2
                return None
    """)

    def test_source_contains_def(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        method = info.classes[0].methods[0]
        assert "def my_method(self) -> None:" in method.source

    def test_source_contains_body(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        method = info.classes[0].methods[0]
        assert "x = 1" in method.source
        assert "y = 2" in method.source
        assert "return None" in method.source

    def test_source_line_range(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        method = info.classes[0].methods[0]
        assert method.line_start == 2
        assert method.line_end == 5


class TestImports:
    """Parse ``import os`` and ``from typing import Any``, verify imports list."""

    SOURCE = textwrap.dedent("""\
        import os
        import sys
        from typing import Any, Optional
        from pathlib import Path
    """)

    def test_import_count(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert len(info.imports) == 4

    def test_import_statements(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        assert "import os" in info.imports
        assert "import sys" in info.imports
        assert "from typing import Any, Optional" in info.imports
        assert "from pathlib import Path" in info.imports


class TestNestedClassIgnored:
    """Verify only top-level classes are extracted."""

    SOURCE = textwrap.dedent("""\
        class Outer:
            class Inner:
                pass

            def method(self):
                pass

        class TopLevel:
            pass
    """)

    def test_only_top_level_classes(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        names = [c.name for c in info.classes]
        assert "Outer" in names
        assert "TopLevel" in names
        # Inner is NOT a top-level class
        assert "Inner" not in names

    def test_outer_has_method(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        outer = [c for c in info.classes if c.name == "Outer"][0]
        # Inner shows up as a nested class in the body but extract_class only
        # extracts methods (FunctionDef/AsyncFunctionDef), so only "method" appears
        method_names = [m.name for m in outer.methods]
        assert "method" in method_names


class TestRealPipecatSnippet:
    """Parse a realistic pipecat-style class."""

    def test_module_docstring(self):
        info = extract_module_info(PIPECAT_SNIPPET, "pipecat.frames.frames")
        assert info.docstring == "Frame types for the Pipecat framework."

    def test_classes_extracted(self):
        info = extract_module_info(PIPECAT_SNIPPET, "pipecat.frames.frames")
        names = [c.name for c in info.classes]
        assert "Frame" in names
        assert "TTSService" in names

    def test_frame_is_dataclass(self):
        info = extract_module_info(PIPECAT_SNIPPET, "pipecat.frames.frames")
        frame = [c for c in info.classes if c.name == "Frame"][0]
        assert frame.is_dataclass is True
        assert frame.docstring == "Base frame type."

    def test_tts_service_methods(self):
        info = extract_module_info(PIPECAT_SNIPPET, "pipecat.frames.frames")
        tts = [c for c in info.classes if c.name == "TTSService"][0]
        method_names = [m.name for m in tts.methods]
        assert "__init__" in method_names
        assert "run_tts" in method_names

    def test_init_keyword_only_params(self):
        info = extract_module_info(PIPECAT_SNIPPET, "pipecat.frames.frames")
        tts = [c for c in info.classes if c.name == "TTSService"][0]
        init = [m for m in tts.methods if m.name == "__init__"][0]
        param_names = [p.name for p in init.parameters]
        assert "self" in param_names
        assert "aggregate_sentences" in param_names
        assert "push_stop_frames" in param_names

        agg = [p for p in init.parameters if p.name == "aggregate_sentences"][0]
        assert agg.annotation == "bool"
        assert agg.default == "True"

        push = [p for p in init.parameters if p.name == "push_stop_frames"][0]
        assert push.annotation == "bool"
        assert push.default == "False"

    def test_run_tts_is_async(self):
        info = extract_module_info(PIPECAT_SNIPPET, "pipecat.frames.frames")
        tts = [c for c in info.classes if c.name == "TTSService"][0]
        run_tts = [m for m in tts.methods if m.name == "run_tts"][0]
        assert run_tts.return_type == "None"
        assert run_tts.docstring == "Run TTS. Override in subclass."
        # Source should contain "async def"
        assert "async def run_tts" in run_tts.source

    def test_imports_extracted(self):
        info = extract_module_info(PIPECAT_SNIPPET, "pipecat.frames.frames")
        assert "from dataclasses import dataclass" in info.imports

    def test_tts_service_base_classes(self):
        info = extract_module_info(PIPECAT_SNIPPET, "pipecat.frames.frames")
        tts = [c for c in info.classes if c.name == "TTSService"][0]
        # TTSService has no base classes in the snippet
        assert tts.base_classes == []

    def test_tts_service_docstring(self):
        info = extract_module_info(PIPECAT_SNIPPET, "pipecat.frames.frames")
        tts = [c for c in info.classes if c.name == "TTSService"][0]
        assert tts.docstring == "Text-to-speech service base class."


class TestKwOnlyWithStarSeparator:
    """Ensure keyword-only params (after bare *) are extracted correctly."""

    SOURCE = textwrap.dedent("""\
        def connect(*, host: str = "localhost", port: int = 8080) -> None:
            pass
    """)

    def test_star_separator_present(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        param_names = [p.name for p in func.parameters]
        assert "*" in param_names

    def test_kwonly_params(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        host = [p for p in func.parameters if p.name == "host"][0]
        assert host.annotation == "str"
        assert host.default == "'localhost'"

        port = [p for p in func.parameters if p.name == "port"][0]
        assert port.annotation == "int"
        assert port.default == "8080"


class TestPosOnlyWithSlashSeparator:
    """Ensure positional-only params (before /) are extracted with separator."""

    SOURCE = textwrap.dedent("""\
        def f(a: int, b: int, /, c: str = "x") -> None:
            pass
    """)

    def test_slash_separator_present(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        param_names = [p.name for p in func.parameters]
        assert "/" in param_names

    def test_posonly_before_slash(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        param_names = [p.name for p in func.parameters]
        slash_idx = param_names.index("/")
        assert param_names[:slash_idx] == ["a", "b"]

    def test_regular_after_slash(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        param_names = [p.name for p in func.parameters]
        slash_idx = param_names.index("/")
        assert "c" in param_names[slash_idx + 1:]

    def test_posonly_annotations(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        a = [p for p in func.parameters if p.name == "a"][0]
        assert a.annotation == "int"
        c = [p for p in func.parameters if p.name == "c"][0]
        assert c.annotation == "str"
        assert c.default == "'x'"

    def test_signature_includes_slash(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        sig = build_signature(func.name, func.parameters, func.return_type)
        assert sig == "(a: int, b: int, /, c: str = 'x') -> None"


class TestPosOnlyAndKwOnly:
    """Ensure both / and * separators work together."""

    SOURCE = textwrap.dedent("""\
        def g(a: int, /, b: str, *, c: bool = True) -> None:
            pass
    """)

    def test_both_separators_present(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        param_names = [p.name for p in func.parameters]
        assert "/" in param_names
        assert "*" in param_names

    def test_order(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        param_names = [p.name for p in func.parameters]
        assert param_names == ["a", "/", "b", "*", "c"]

    def test_signature(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        sig = build_signature(func.name, func.parameters, func.return_type)
        assert sig == "(a: int, /, b: str, *, c: bool = True) -> None"


class TestPosOnlyOnly:
    """All params are positional-only (no regular params after /)."""

    SOURCE = textwrap.dedent("""\
        def h(x: int, y: int, /) -> int:
            return x + y
    """)

    def test_slash_at_end(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        param_names = [p.name for p in func.parameters]
        assert param_names == ["x", "y", "/"]

    def test_signature(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        sig = build_signature(func.name, func.parameters, func.return_type)
        assert sig == "(x: int, y: int, /) -> int"


class TestFunctionSourceExtraction:
    """Verify top-level function source is extracted correctly."""

    SOURCE = textwrap.dedent("""\
        def add(a: int, b: int) -> int:
            \"\"\"Add two numbers.\"\"\"
            return a + b
    """)

    def test_source(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        assert "def add(a: int, b: int) -> int:" in func.source
        assert "return a + b" in func.source

    def test_line_range(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        func = info.functions[0]
        assert func.line_start == 1
        assert func.line_end == 3


# ---------------------------------------------------------------------------
# Yield extraction tests
# ---------------------------------------------------------------------------


class TestYieldExtraction:
    """Verify yield/yield-from frame type extraction."""

    SOURCE = textwrap.dedent('''\
        class TTSService:
            async def run_tts(self, text: str):
                """Generate TTS audio."""
                audio = self._synthesize(text)
                yield TTSAudioRawFrame(audio=audio, sample_rate=16000)
                yield TTSStoppedFrame()
    ''')

    def test_yields_extracted(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        method = info.classes[0].methods[0]
        assert method.yields == ["TTSAudioRawFrame", "TTSStoppedFrame"]

    def test_bare_yield_skipped(self):
        """Bare ``yield variable`` without Call wrapper is skipped."""
        source = textwrap.dedent('''\
            class Proc:
                def gen(self):
                    frame = make_frame()
                    yield frame
                    yield None
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert method.yields == []

    def test_yield_from_excluded(self):
        """``yield from gen()`` is excluded — generator name is not a frame type."""
        source = textwrap.dedent('''\
            class Proc:
                def gen(self):
                    yield from generate_frames()
                    yield from FrameFactory.create()
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert method.yields == []

    def test_no_yields_empty_list(self):
        """Methods without yields have an empty yields list."""
        source = textwrap.dedent('''\
            class Proc:
                def process(self, x):
                    return x + 1
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert method.yields == []

    def test_duplicate_yields_deduplicated(self):
        """Same frame type yielded multiple times appears once."""
        source = textwrap.dedent('''\
            class Proc:
                def gen(self):
                    yield AudioFrame(data=b"a")
                    yield AudioFrame(data=b"b")
                    yield StopFrame()
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert method.yields == ["AudioFrame", "StopFrame"]


# ---------------------------------------------------------------------------
# Call extraction tests
# ---------------------------------------------------------------------------


class TestCallExtraction:
    """Verify method call extraction from function bodies."""

    SOURCE = textwrap.dedent('''\
        class MyProcessor(FrameProcessor):
            async def process_frame(self, frame):
                await super().process_frame(frame)
                result = self.transform(frame)
                await self.push_frame(OutputFrame(data=result))
                logger.info("done")
    ''')

    def test_self_calls(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        method = info.classes[0].methods[0]
        assert "transform" in method.calls
        assert "push_frame" in method.calls

    def test_super_call(self):
        info = extract_module_info(self.SOURCE, "test_mod")
        method = info.classes[0].methods[0]
        assert "super().process_frame" in method.calls

    def test_lowercase_attribute_excluded(self):
        """logger.info() should NOT be captured (lowercase first char)."""
        info = extract_module_info(self.SOURCE, "test_mod")
        method = info.classes[0].methods[0]
        assert "info" not in method.calls
        assert "logger.info" not in method.calls

    def test_class_method_call(self):
        """ClassName.method() pattern is captured."""
        source = textwrap.dedent('''\
            class Foo:
                def bar(self):
                    result = Helper.convert(data)
                    return result
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert "Helper.convert" in method.calls

    def test_await_self_call(self):
        """``await self.method()`` is captured (ast.walk traverses into Await)."""
        source = textwrap.dedent('''\
            class Svc:
                async def run(self):
                    await self.start()
                    await self.push_frame(Frame())
                    return None
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert "start" in method.calls
        assert "push_frame" in method.calls

    def test_no_calls_empty_list(self):
        """Methods without relevant calls have an empty list."""
        source = textwrap.dedent('''\
            class Foo:
                def bar(self):
                    x = len([1, 2, 3])
                    return x
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert method.calls == []

    def test_duplicate_calls_deduplicated(self):
        """Same method called multiple times appears once."""
        source = textwrap.dedent('''\
            class Foo:
                def bar(self):
                    self.push(1)
                    self.push(2)
                    self.push(3)
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert method.calls == ["push"]

    def test_private_method_call(self):
        """self._private_method() is captured."""
        source = textwrap.dedent('''\
            class Svc:
                def run(self):
                    self._setup()
                    self.__internal()
                    return None
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert "_setup" in method.calls
        assert "__internal" in method.calls

    def test_top_level_function_calls(self):
        """Top-level functions also extract calls."""
        source = textwrap.dedent('''\
            def helper():
                result = Manager.process(data)
                return result
        ''')
        info = extract_module_info(source, "test_mod")
        func = info.functions[0]
        assert "Manager.process" in func.calls


class TestNestedFunctionBoundary:
    """Regression: yields/calls from nested functions must not leak to the outer function."""

    SOURCE = textwrap.dedent('''\
        class Processor:
            def outer(self):
                """Outer method — only calls self.setup()."""
                self.setup()

                def inner_helper():
                    """Nested helper — calls self.push_frame() and yields AudioFrame."""
                    self.push_frame(AudioFrame())
                    yield AudioFrame(data=b"x")

                return inner_helper
    ''')

    def test_outer_calls_exclude_inner(self):
        """outer() should have calls=['setup'], not push_frame from inner."""
        info = extract_module_info(self.SOURCE, "test_mod")
        outer = info.classes[0].methods[0]
        assert outer.name == "outer"
        assert "setup" in outer.calls
        assert "push_frame" not in outer.calls

    def test_outer_yields_exclude_inner(self):
        """outer() should have yields=[], not AudioFrame from inner."""
        info = extract_module_info(self.SOURCE, "test_mod")
        outer = info.classes[0].methods[0]
        assert outer.yields == []

    def test_nested_async_excluded(self):
        """Nested async def should not leak calls to outer."""
        source = textwrap.dedent('''\
            class Svc:
                async def run(self):
                    self.start()

                    async def on_event():
                        await self.handle_event()
                        yield EventFrame()

                    return on_event
        ''')
        info = extract_module_info(source, "test_mod")
        run_method = info.classes[0].methods[0]
        assert run_method.name == "run"
        assert "start" in run_method.calls
        assert "handle_event" not in run_method.calls
        assert run_method.yields == []

    def test_lambda_calls_excluded(self):
        """Lambda body calls should not leak to the enclosing method."""
        source = textwrap.dedent('''\
            class Svc:
                def setup(self):
                    self.start()
                    cb = lambda: self.push_frame(Frame())
                    return cb
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert method.name == "setup"
        assert "start" in method.calls
        assert "push_frame" not in method.calls

    def test_lambda_yields_excluded(self):
        """Lambda body yields should not leak to the enclosing function."""
        source = textwrap.dedent('''\
            def outer():
                gen = lambda: (yield AudioFrame())
                return gen
        ''')
        info = extract_module_info(source, "test_mod")
        func = info.functions[0]
        assert func.yields == []

    def test_comprehension_calls_included(self):
        """Calls inside comprehensions ARE included (intentional — part of method logic)."""
        source = textwrap.dedent('''\
            class Proc:
                def run(self):
                    results = [self.transform(x) for x in items]
                    return results
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert "transform" in method.calls

    def test_yield_from_variable_skipped(self):
        """``yield from self._frames`` (non-Call value) is skipped."""
        source = textwrap.dedent('''\
            class Proc:
                def gen(self):
                    yield from self._frames
        ''')
        info = extract_module_info(source, "test_mod")
        assert info.classes[0].methods[0].yields == []

    def test_chained_attribute_call_excluded(self):
        """``self.get_transport().send()`` — the chained .send() is not captured."""
        source = textwrap.dedent('''\
            class Svc:
                def run(self):
                    self.get_transport().send(data)
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        # self.get_transport() is a self.method() call — captured.
        assert "get_transport" in method.calls
        # .send() is chained (func.value is a Call, not Name) — excluded.
        assert "send" not in method.calls


class TestDecoratorAndDefaultExclusion:
    """Regression: calls/yields in decorators, defaults, and annotations must not leak."""

    def test_decorator_calls_excluded(self):
        """Calls in decorators should not appear in method calls."""
        source = textwrap.dedent('''\
            class Svc:
                @Router.route("/path")
                def handle(self):
                    self.process()
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert "process" in method.calls
        assert "Router.route" not in method.calls
        assert "route" not in method.calls

    def test_default_value_calls_excluded(self):
        """Calls in parameter defaults should not appear in method calls."""
        source = textwrap.dedent('''\
            class Svc:
                def run(self, config=Config.default()):
                    self.start()
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert "start" in method.calls
        assert "Config.default" not in method.calls

    def test_return_annotation_excluded(self):
        """Calls in return annotations should not appear in method calls."""
        source = textwrap.dedent('''\
            class Svc:
                def run(self) -> Optional[Frame]:
                    self.start()
                    return None
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert "start" in method.calls
        # Optional[Frame] is an annotation, not a runtime call
        assert method.calls == ["start"]


class TestCallExtractionOrder:
    """Verify calls/yields preserve source order."""

    def test_calls_in_source_order(self):
        """Calls should appear in the order they first occur in source."""
        source = textwrap.dedent('''\
            class Proc:
                def run(self):
                    self.first()
                    if True:
                        self.second()
                    self.third()
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert method.calls == ["first", "second", "third"]

    def test_yields_in_source_order(self):
        """Yields should appear in the order they first occur in source."""
        source = textwrap.dedent('''\
            class Gen:
                def run(self):
                    yield AlphaFrame()
                    if True:
                        yield BetaFrame()
                    yield GammaFrame()
        ''')
        info = extract_module_info(source, "test_mod")
        method = info.classes[0].methods[0]
        assert method.yields == ["AlphaFrame", "BetaFrame", "GammaFrame"]
