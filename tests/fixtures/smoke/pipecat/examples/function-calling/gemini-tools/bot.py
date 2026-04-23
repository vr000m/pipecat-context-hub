"""Function-calling demo: Gemini tools."""

from pipecat.pipeline import Pipeline
from pipecat.services.google import GoogleLLMService


def main() -> None:
    llm = GoogleLLMService()
    Pipeline([llm])
