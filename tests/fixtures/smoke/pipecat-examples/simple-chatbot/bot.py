"""Simple chatbot example — pipecat-examples root-level layout."""

from pipecat.pipeline import Pipeline
from pipecat.services.openai import OpenAILLMService


def main() -> None:
    llm = OpenAILLMService()
    Pipeline([llm])


if __name__ == "__main__":
    main()
