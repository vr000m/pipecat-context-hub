"""MCP demo: connect to a remote MCP server for tool use."""

from pipecat.pipeline import Pipeline
from pipecat.services.openai import OpenAILLMService


def main() -> None:
    llm = OpenAILLMService()
    Pipeline([llm])
