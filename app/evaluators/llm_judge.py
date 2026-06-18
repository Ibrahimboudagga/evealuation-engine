import re
import structlog
from typing import Any, Dict
from pydantic import BaseModel, Field, ValidationError
from json_repair import repair_json

from app.evaluators.base import BaseEvaluator
from app.providers.base import BaseProvider
from app.schemas.result import EvaluationResult

log = structlog.get_logger()

class JudgeResponse(BaseModel):
    score: int = Field(..., ge=1, le=10, description="Evaluation score between 1 and 10")
    reason: str = Field(..., description="Justification for the assigned score")

class LLMAsAJudgeEvaluator(BaseEvaluator):
    """
    Evaluator that uses an LLM to assess a prediction based on expected output,
    returning a score from 1-10 (which is normalized to 0.0 - 1.0).
    """

    DEFAULT_PROMPT_TEMPLATE = """You are an objective AI evaluator. Your task is to grade the performance of an assistant model prediction against a target expected output.

Input context:
{input}

Expected output (Ground Truth):
{expected_output}

Model prediction:
{prediction}

Evaluate how well the model prediction satisfies the prompt input and matches the expected output.
Provide a grade from 1 to 10, where 1 is completely wrong, irrelevant or empty, and 10 is perfectly correct, complete, and high-quality.

You MUST reply ONLY with a JSON object in this format (no markdown formatting, no extra text):
{{
  "score": <int between 1 and 10>,
  "reason": "<detailed explanation of your reasoning>"
}}
"""

    def __init__(self, judge_provider: BaseProvider, name_suffix: str = "llm_judge", prompt_template: str | None = None):
        self.provider = judge_provider
        self._name = name_suffix
        self.prompt_template = prompt_template or self.DEFAULT_PROMPT_TEMPLATE

    @property
    def name(self) -> str:
        return self._name

    def _extract_and_parse_json(self, text: str) -> Dict[str, Any]:
        """Extracts JSON structure from text, even if wrapped in markdown blocks.
        Uses json_repair to handle malformed LLM output gracefully."""
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
        raise ValueError(f"Unable to parse JSON from LLM response: {text[:200]}")

    async def evaluate(
        self, 
        input_text: str, 
        expected_output: str, 
        prediction: str
    ) -> EvaluationResult:
        
        prompt = self.prompt_template.format(
            input=input_text,
            expected_output=expected_output,
            prediction=prediction,
        )
        
        raw_response = ""
        try:
            raw_response, usage = await self.provider.generate(prompt)
            parsed_data = self._extract_and_parse_json(raw_response)
            judge_res = JudgeResponse.model_validate(parsed_data)
            
            # Normalize 1-10 to 0.0-1.0
            normalized_score = judge_res.score / 10.0
            
            return EvaluationResult(
                example_id="",  # Populated by the runner
                prompt=input_text,
                prediction=prediction,
                expected_output=expected_output,
                score=normalized_score,
                evaluator_name=self.name,
                metadata={
                    "raw_score": judge_res.score,
                    "reason": judge_res.reason
                },
                prompt_tokens=usage.get("prompt_tokens") if usage else None,
                completion_tokens=usage.get("completion_tokens") if usage else None,
            )
            
        except Exception as e:
            log.error("llm_judge_evaluation_failed", raw_response=raw_response, error=str(e))
            
            # Graceful error handling: return score 0.0 but capture the failure in metadata
            return EvaluationResult(
                example_id="",
                prompt=input_text,
                prediction=prediction,
                expected_output=expected_output,
                score=0.0,
                evaluator_name=self.name,
                metadata={
                    "error": str(e),
                    "raw_response": raw_response,
                    "reason": "Failed to parse judge output or generate response"
                }
            )
