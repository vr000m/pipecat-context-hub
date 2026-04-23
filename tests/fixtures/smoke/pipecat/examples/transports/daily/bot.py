"""Transports demo: Daily WebRTC transport."""

from pipecat.pipeline import Pipeline
from pipecat.transports.services.daily import DailyTransport


def main() -> None:
    t = DailyTransport()
    Pipeline([t])
