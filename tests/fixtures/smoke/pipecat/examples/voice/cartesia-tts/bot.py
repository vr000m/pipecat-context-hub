"""Voice demo: Cartesia TTS."""

from pipecat.pipeline import Pipeline
from pipecat.services.cartesia import CartesiaTTSService


def main() -> None:
    tts = CartesiaTTSService()
    Pipeline([tts])
