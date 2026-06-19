import re
import structlog
from typing import Any, Dict
from pydantic import BaseModel, Field

from json_repair import repair_json

from app.evaluators.base_pairwise import BasePairwiseEvaluator, PairwiseComparisonResult
from app.providers.base import BaseProvider

log = structlog.get_logger()


class PairwiseJudgeResponse(BaseModel):
    """Validation model for the judge LLM's JSON output."""
    winner: str = Field(..., description="'A', 'B', or 'tie'")
    score_a: int = Field(..., ge=1, le=10, description="Score for response A (1-10)")
    score_b: int = Field(..., ge=1, le=10, description="Score for response B (1-10)")
    reason: str = Field(..., description="Explanation for the comparison outcome")


class PairwiseJudgeEvaluator(BasePairwiseEvaluator):
    """
    Pairwise evaluator that uses an LLM to compare two model responses side-by-side.
    Returns a winner (A/B/tie), individual scores, and reasoning.
    """

    DEFAULT_PROMPT_TEMPLATE = """You are an impartial AI judge evaluating two model responses side by side.

Input:
{input}

Expected Output (Ground Truth):
{expected_output}

Response A:
{response_a}

Response B:
{response_b}

Compare both responses against the expected output. Consider correctness, completeness, and quality.

Score each response from 1 to 10 (1 = completely wrong, 10 = perfectly correct).
Declare a winner: "A" if A is better, "B" if B is better, or "tie" if scores are within 1 point of each other.

You MUST reply ONLY with a JSON object in this format (no markdown formatting, no extra text):
{{
  "winner": "A" or "B" or "tie",
  "score_a": <int between 1 and 10>,
  "score_b": <int between 1 and 10>,
  "reason": "<detailed explanation of your comparison>"
}}
"""

    def __init__(
        self,
        judge_provider: BaseProvider,
        prompt_template: str | None = None,
    ):
        self.provider = judge_provider
        self.prompt_template = prompt_template or self.DEFAULT_PROMPT_TEMPLATE

    @property
    def name(self) -> str:
        return "pairwise_judge"

    def _extract_and_parse_json(self, text: str) -> Dict[str, Any]:
        """Extracts JSON structure from text, handling markdown blocks and malformed output."""
        text = text.strip()

        # Try finding markdown JSON block
        json_block_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if json_block_match:
            result = repair_json(json_block_match.group(1), return_objects=True)
            if isinstance(result, dict):
                return result

        # Try finding anything between the first '{' and last '}'
        brace_match = re.search(r"(\{.*\})", text, re.DOTALL)
        if brace_match:
            result = repair_json(brace_match.group(1), return_objects=True)
            if isinstance(result, dict):
                return result

        # Fallback to direct repair
        result = repair_json(text, return_objects=True)
        if isinstance(result, dict):
            return result
        raise ValueError(f"Unable to parse JSON from judge response: {text[:200]}")

    async def evaluate(
        self,
        input_text: str,
        expected_output: str,
        response_a: str,
        response_b: str,
    ) -> PairwiseComparisonResult:
        prompt = self.prompt_template.format(
            input=input_text,
            expected_output=expected_output,
            response_a=response_a,
            response_b=response_b,
        )

        raw_response = ""
        try:
            raw_response, usage = await self.provider.generate(prompt)
            parsed_data = self._extract_and_parse_json(raw_response)
            judge_res = PairwiseJudgeResponse.model_validate(parsed_data)

            # Normalize winner to canonical form
            winner = judge_res.winner.strip().upper()
            if winner not in ("A", "B", "TIE"):
                winner = "tie"

            # Normalize scores from 1-10 to 0.0-1.0
            normalized_a = judge_res.score_a / 10.0
            normalized_b = judge_res.score_b / 10.0

            return PairwiseComparisonResult(
                winner=winner,
                score_a=normalized_a,
                score_b=normalized_b,
                reason=judge_res.reason,
                metadata={
                    "raw_score_a": judge_res.score_a,
                    "raw_score_b": judge_res.score_b,
                    "raw_winner": judge_res.winner,
                    "prompt_tokens": usage.get("prompt_tokens") if usage else None,
                    "completion_tokens": usage.get("completion_tokens") if usage else None,
                },
            )

        except Exception as e:
            log.error("pairwise_judge_evaluation_failed", raw_response=raw_response, error=str(e))
            return PairwiseComparisonResult(
                winner="tie",
                score_a=0.0,
                score_b=0.0,
                reason=f"Judge evaluation failed: {str(e)}",
                metadata={
                    "error": str(e),
                    "raw_response": raw_response,
                },
            )
