"""Storytelling bot — example at pipecat-examples root."""

from pipecat.pipeline import Pipeline
from pipecat.services.elevenlabs import ElevenLabsTTSService


def main() -> None:
    tts = ElevenLabsTTSService()
    Pipeline([tts])
