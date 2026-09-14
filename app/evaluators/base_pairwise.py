from abc import ABC, abstractmethod
from typing import Any, Dict, Literal, Optional
from pydantic import BaseModel, Field

from app.schemas.outcomes import EvaluationOutcome


class PairwiseComparisonResult(BaseModel):
    """Result of a single pairwise comparison between two model responses."""
    winner: Optional[Literal["A", "B", "tie"]] = Field(default=None, description="Which response won, or null when judging failed")
    score_a: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Normalized score for response A")
    score_b: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Normalized score for response B")
    reason: str = Field(..., description="Judge's explanation for the comparison outcome")
    outcome: EvaluationOutcome = Field(default=EvaluationOutcome.EVALUATED, description="Whether judging completed or failed")
    error_message: Optional[str] = Field(default=None, description="Sanitized error when judging did not complete")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Optional metadata")


class BasePairwiseEvaluator(ABC):
    """Abstract base class for pairwise evaluation metrics."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Returns the unique name of the pairwise evaluator."""
        pass

    @abstractmethod
    async def evaluate(
        self,
        input_text: str,
        expected_output: str,
        response_a: str,
        response_b: str,
    ) -> PairwiseComparisonResult:
        """
        Compares two model responses for the same input.

        Args:
            input_text: The original prompt/input.
            expected_output: The ground truth / expected answer.
            response_a: Response from model A.
            response_b: Response from model B.

        Returns:
            A PairwiseComparisonResult with winner, scores, and reasoning.
        """
        pass
