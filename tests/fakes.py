"""Deterministic provider doubles for evaluation tests.

These providers never use credentials or make network calls. Choose a mode to
exercise success, judge parsing, malformed output, or provider failures.
"""

from enum import Enum
import json
from typing import Any, Dict, Optional, Tuple

from app.providers.base import BaseProvider


class FakeProviderMode(str, Enum):
    SUCCESS = "success"
    VALID_JUDGE = "valid_judge"
    VALID_PAIRWISE_JUDGE = "valid_pairwise_judge"
    VALID_PAIRWISE_TIE = "valid_pairwise_tie"
    INVALID_PAIRWISE_WINNER = "invalid_pairwise_winner"
    MALFORMED_JSON = "malformed_json"
    ERROR = "error"


class DeterministicFakeProvider(BaseProvider):
    """A configurable provider whose behavior is stable across test runs."""

    is_mock = True

    def __init__(self, mode: FakeProviderMode = FakeProviderMode.SUCCESS):
        self.mode = mode
        self.model_name = f"deterministic-fake-{mode.value}"
        self.prompts: list[str] = []

    @classmethod
    def successful(cls) -> "DeterministicFakeProvider":
        return cls(FakeProviderMode.SUCCESS)

    @classmethod
    def valid_judge(cls) -> "DeterministicFakeProvider":
        return cls(FakeProviderMode.VALID_JUDGE)

    @classmethod
    def valid_pairwise_judge(cls) -> "DeterministicFakeProvider":
        return cls(FakeProviderMode.VALID_PAIRWISE_JUDGE)

    @classmethod
    def valid_pairwise_tie(cls) -> "DeterministicFakeProvider":
        return cls(FakeProviderMode.VALID_PAIRWISE_TIE)

    @classmethod
    def invalid_pairwise_winner(cls) -> "DeterministicFakeProvider":
        return cls(FakeProviderMode.INVALID_PAIRWISE_WINNER)

    @classmethod
    def malformed_json(cls) -> "DeterministicFakeProvider":
        return cls(FakeProviderMode.MALFORMED_JSON)

    @classmethod
    def failing(cls) -> "DeterministicFakeProvider":
        return cls(FakeProviderMode.ERROR)

    async def generate(self, prompt: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        self.prompts.append(prompt)

        if self.mode is FakeProviderMode.ERROR:
            raise RuntimeError("deterministic fake provider failure")

        if self.mode is FakeProviderMode.VALID_JUDGE:
            return json.dumps({"score": 8, "reason": "deterministic judge response"}), {
                "prompt_tokens": 11,
                "completion_tokens": 7,
            }

        if self.mode is FakeProviderMode.VALID_PAIRWISE_JUDGE:
            return json.dumps(
                {
                    "winner": "A",
                    "score_a": 9,
                    "score_b": 4,
                    "reason": "deterministic pairwise judge response",
                }
            ), {"prompt_tokens": 13, "completion_tokens": 9}

        if self.mode is FakeProviderMode.VALID_PAIRWISE_TIE:
            return json.dumps(
                {
                    "winner": "tie",
                    "score_a": 7,
                    "score_b": 7,
                    "reason": "deterministic pairwise tie",
                }
            ), {"prompt_tokens": 13, "completion_tokens": 9}

        if self.mode is FakeProviderMode.INVALID_PAIRWISE_WINNER:
            return json.dumps(
                {
                    "winner": "draw",
                    "score_a": 7,
                    "score_b": 7,
                    "reason": "invalid winner label",
                }
            ), {"prompt_tokens": 13, "completion_tokens": 9}

        if self.mode is FakeProviderMode.MALFORMED_JSON:
            return "this is deliberately not JSON", {"prompt_tokens": 5, "completion_tokens": 3}

        return "deterministic successful answer", {"prompt_tokens": 3, "completion_tokens": 2}
