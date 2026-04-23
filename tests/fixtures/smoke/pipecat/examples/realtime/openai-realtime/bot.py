"""Realtime voice-to-voice demo using OpenAI Realtime API."""

from pipecat.pipeline import Pipeline
from pipecat.services.openai import OpenAIRealtimeService


def main() -> None:
    svc = OpenAIRealtimeService()
    Pipeline([svc])
