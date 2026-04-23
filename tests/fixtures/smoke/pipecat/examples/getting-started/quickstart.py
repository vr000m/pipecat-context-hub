"""Another flat-file example under the getting-started topic."""

from pipecat.pipeline import Pipeline
from pipecat.transports import BaseTransport


def run() -> None:
    _ = BaseTransport
    Pipeline()
