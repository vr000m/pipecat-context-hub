"""Voice bot example for pipecat-examples layout."""

from pipecat.pipeline import Pipeline
from pipecat.services.deepgram import DeepgramSTTService


def main() -> None:
    stt = DeepgramSTTService()
    Pipeline([stt])
