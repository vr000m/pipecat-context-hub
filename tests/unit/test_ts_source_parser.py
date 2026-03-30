"""Tests for TypeScript source parser."""

from __future__ import annotations

import textwrap

import pytest

from pipecat_context_hub.services.ingest.ts_source_parser import (
    TsDeclaration,
    parse_ts_source,
)


# ---------------------------------------------------------------------------
# Interface parsing
# ---------------------------------------------------------------------------


class TestInterfaceParsing:
    def test_simple_interface(self) -> None:
        source = textwrap.dedent("""\
            export interface Config {
              url: string;
              token: string;
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        d = decls[0]
        assert d.name == "Config"
        assert d.kind == "interface"
        assert d.chunk_type == "class_overview"
        assert d.base_classes == []

    def test_interface_extends(self) -> None:
        source = textwrap.dedent("""\
            export interface ExtendedConfig extends BaseConfig, Serializable {
              extra: number;
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].base_classes == ["BaseConfig", "Serializable"]

    def test_interface_with_generics(self) -> None:
        source = textwrap.dedent("""\
            export interface Response<T> {
              data: T;
              error: string | null;
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].name == "Response"

    def test_default_export_interface(self) -> None:
        source = textwrap.dedent("""\
            export default interface AppConfig {
              debug: boolean;
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].name == "AppConfig"

    def test_interface_with_nested_braces(self) -> None:
        source = textwrap.dedent("""\
            export interface Config {
              callbacks: {
                onReady: () => void;
                onError: (err: Error) => void;
              };
              name: string;
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert "name: string" in decls[0].body


# ---------------------------------------------------------------------------
# Class parsing
# ---------------------------------------------------------------------------


class TestClassParsing:
    def test_simple_class(self) -> None:
        source = textwrap.dedent("""\
            export class PipecatClient {
              private url: string;
              constructor(url: string) {
                this.url = url;
              }
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        d = decls[0]
        assert d.name == "PipecatClient"
        assert d.kind == "class"
        assert d.chunk_type == "class_overview"
        assert d.is_abstract is False

    def test_abstract_class(self) -> None:
        source = textwrap.dedent("""\
            export abstract class Transport {
              abstract initDevices(): Promise<void>;
              abstract send(data: any): void;
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].is_abstract is True

    def test_class_extends_implements(self) -> None:
        source = textwrap.dedent("""\
            export class WebSocketTransport extends Transport implements Serializable {
              connect(): void {}
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].base_classes == ["Transport", "Serializable"]

    def test_class_extends_with_generics(self) -> None:
        source = textwrap.dedent("""\
            export class RTVIEventEmitter<T extends Record<string, unknown>> extends EventEmitter {
              emit(event: string): void {}
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].name == "RTVIEventEmitter"
        assert decls[0].base_classes == ["EventEmitter"]


# ---------------------------------------------------------------------------
# Type alias parsing
# ---------------------------------------------------------------------------


class TestTypeAliasParsing:
    def test_simple_type(self) -> None:
        source = 'export type ID = string;\n'
        decls = parse_ts_source(source)
        assert len(decls) == 1
        d = decls[0]
        assert d.name == "ID"
        assert d.kind == "type_alias"
        assert d.chunk_type == "type_definition"

    def test_union_type(self) -> None:
        source = textwrap.dedent("""\
            export type TransportState =
              | "disconnected"
              | "connecting"
              | "connected"
              | "error";
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert '"disconnected"' in decls[0].body

    def test_object_type(self) -> None:
        source = textwrap.dedent("""\
            export type Options = {
              url: string;
              token: string;
              timeout: number;
            };
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert "timeout: number" in decls[0].body

    def test_generic_type(self) -> None:
        source = 'export type Callback<T> = (data: T) => void;\n'
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].name == "Callback"


# ---------------------------------------------------------------------------
# Function parsing
# ---------------------------------------------------------------------------


class TestFunctionParsing:
    def test_simple_function(self) -> None:
        source = textwrap.dedent("""\
            export function createClient(url: string): PipecatClient {
              return new PipecatClient(url);
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        d = decls[0]
        assert d.name == "createClient"
        assert d.kind == "function"
        assert d.chunk_type == "function"

    def test_async_function(self) -> None:
        source = textwrap.dedent("""\
            export async function connect(url: string): Promise<void> {
              await fetch(url);
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].name == "connect"

    def test_generic_function(self) -> None:
        source = textwrap.dedent("""\
            export function transform<T, U>(data: T, fn: (t: T) => U): U {
              return fn(data);
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].name == "transform"


# ---------------------------------------------------------------------------
# Enum parsing
# ---------------------------------------------------------------------------


class TestEnumParsing:
    def test_simple_enum(self) -> None:
        source = textwrap.dedent("""\
            export enum RTVIEvent {
              Connected = "connected",
              Disconnected = "disconnected",
              TransportStateChanged = "transportStateChanged",
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        d = decls[0]
        assert d.name == "RTVIEvent"
        assert d.kind == "enum"
        assert d.chunk_type == "type_definition"

    def test_const_enum(self) -> None:
        source = textwrap.dedent("""\
            export const enum Direction {
              Up,
              Down,
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].name == "Direction"


# ---------------------------------------------------------------------------
# Typed const export parsing
# ---------------------------------------------------------------------------


class TestConstExportParsing:
    def test_typed_const(self) -> None:
        source = textwrap.dedent("""\
            export const DEFAULT_TIMEOUT: number = 30000;
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        d = decls[0]
        assert d.name == "DEFAULT_TIMEOUT"
        assert d.kind == "const"
        assert d.chunk_type == "function"

    def test_react_component_const(self) -> None:
        source = textwrap.dedent("""\
            export const VoiceVisualizer: React.FC<VoiceVisualizerProps> = ({
              color,
              size,
            }) => {
              return <div>visualizer</div>;
            };
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].name == "VoiceVisualizer"

    def test_provider_const(self) -> None:
        source = textwrap.dedent("""\
            export const PipecatClientProvider: React.FC<ProviderProps> = ({
              children,
              client,
            }) => {
              return <Context.Provider value={client}>{children}</Context.Provider>;
            };
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].name == "PipecatClientProvider"


# ---------------------------------------------------------------------------
# JSDoc extraction
# ---------------------------------------------------------------------------


class TestJSDocExtraction:
    def test_multiline_jsdoc(self) -> None:
        source = textwrap.dedent("""\
            /**
             * A transport for WebSocket connections.
             * Handles bidirectional communication.
             */
            export class WebSocketTransport {
              url: string;
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert "A transport for WebSocket connections." in decls[0].jsdoc
        assert "Handles bidirectional communication." in decls[0].jsdoc

    def test_single_line_jsdoc(self) -> None:
        source = textwrap.dedent("""\
            /** Simple config type */
            export type Config = { url: string };
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].jsdoc == "Simple config type"

    def test_no_jsdoc(self) -> None:
        source = textwrap.dedent("""\
            // Regular comment, not JSDoc
            export interface Foo {
              bar: string;
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].jsdoc == ""

    def test_jsdoc_not_immediately_before(self) -> None:
        source = textwrap.dedent("""\
            /** This comment */

            const unrelated = true;

            export interface Foo {
              bar: string;
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].jsdoc == ""

    def test_jsdoc_with_tags(self) -> None:
        source = textwrap.dedent("""\
            /**
             * Creates a new client.
             * @param url - The server URL
             * @returns A new PipecatClient instance
             */
            export function createClient(url: string): PipecatClient {
              return new PipecatClient(url);
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert "@param url" in decls[0].jsdoc
        assert "@returns" in decls[0].jsdoc

    def test_jsdoc_included_in_snippet(self) -> None:
        source = textwrap.dedent("""\
            /** The main client class */
            export class Client {
              start(): void {}
            }
        """)
        decls = parse_ts_source(source)
        snippet = decls[0].render_snippet("my.module")
        assert "The main client class" in snippet
        assert "Module: my.module" in snippet


# ---------------------------------------------------------------------------
# Non-exported declarations (should be ignored)
# ---------------------------------------------------------------------------


class TestNonExported:
    def test_non_exported_class_ignored(self) -> None:
        source = textwrap.dedent("""\
            class InternalHelper {
              run(): void {}
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 0

    def test_non_exported_function_ignored(self) -> None:
        source = textwrap.dedent("""\
            function helper(x: number): number {
              return x + 1;
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 0

    def test_non_exported_interface_ignored(self) -> None:
        source = textwrap.dedent("""\
            interface InternalConfig {
              debug: boolean;
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 0


# ---------------------------------------------------------------------------
# Snippet rendering
# ---------------------------------------------------------------------------


class TestSnippetRendering:
    def test_class_snippet_includes_bases(self) -> None:
        decl = TsDeclaration(
            name="WebSocket",
            kind="class",
            line_start=1,
            line_end=5,
            body="export class WebSocket extends Transport {}",
            base_classes=["Transport"],
        )
        snippet = decl.render_snippet("transports.websocket")
        assert "# Class: WebSocket" in snippet
        assert "Module: transports.websocket" in snippet
        assert "Extends: Transport" in snippet

    def test_interface_snippet(self) -> None:
        decl = TsDeclaration(
            name="Config",
            kind="interface",
            line_start=1,
            line_end=3,
            body="export interface Config { url: string; }",
        )
        snippet = decl.render_snippet("client.config")
        assert "# Interface: Config" in snippet
        assert "```typescript" in snippet

    def test_abstract_class_snippet(self) -> None:
        decl = TsDeclaration(
            name="Transport",
            kind="class",
            line_start=1,
            line_end=3,
            body="export abstract class Transport {}",
            is_abstract=True,
        )
        snippet = decl.render_snippet("client.transport")
        assert "Abstract: yes" in snippet


# ---------------------------------------------------------------------------
# Mixed file with multiple declarations
# ---------------------------------------------------------------------------


class TestMixedFile:
    def test_multiple_declarations(self) -> None:
        source = textwrap.dedent("""\
            /** Config options */
            export interface ClientOptions {
              url: string;
              timeout: number;
            }

            export type State = "idle" | "running" | "stopped";

            export class Client {
              private state: State = "idle";
              constructor(opts: ClientOptions) {}
            }

            export function createClient(opts: ClientOptions): Client {
              return new Client(opts);
            }

            export enum LogLevel {
              Debug = 0,
              Info = 1,
              Warn = 2,
              Error = 3,
            }

            export const DEFAULT_OPTIONS: ClientOptions = {
              url: "http://localhost",
              timeout: 30000,
            };
        """)
        decls = parse_ts_source(source)
        names = [d.name for d in decls]
        assert "ClientOptions" in names
        assert "State" in names
        assert "Client" in names
        assert "createClient" in names
        assert "LogLevel" in names
        assert "DEFAULT_OPTIONS" in names
        assert len(decls) == 6

    def test_declarations_sorted_by_position(self) -> None:
        source = textwrap.dedent("""\
            export function first(): void {}
            export class Second {}
            export type Third = string;
        """)
        decls = parse_ts_source(source)
        assert decls[0].name == "first"
        assert decls[1].name == "Second"
        assert decls[2].name == "Third"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_source(self) -> None:
        assert parse_ts_source("") == []

    def test_no_exports(self) -> None:
        source = textwrap.dedent("""\
            const x = 1;
            function foo() {}
            class Bar {}
        """)
        assert parse_ts_source(source) == []

    def test_string_containing_braces(self) -> None:
        source = textwrap.dedent("""\
            export class Parser {
              parse(input: string): string {
                return `result: ${input} is {valid}`;
              }
            }
        """)
        decls = parse_ts_source(source)
        assert len(decls) == 1
        assert decls[0].name == "Parser"
