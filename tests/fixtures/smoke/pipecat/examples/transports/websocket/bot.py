"""Transports demo: WebSocket transport."""

from pipecat.pipeline import Pipeline
from pipecat.transports.network.websocket import WebsocketTransport


def main() -> None:
    t = WebsocketTransport()
    Pipeline([t])
