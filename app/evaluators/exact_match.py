from app.evaluators.base import BaseEvaluator
from app.schemas.result import EvaluationResult

class ExactMatchEvaluator(BaseEvaluator):
    """
    An evaluator that performs case-insensitive, whitespace-trimmed exact string matching.
    """
    
    @property
    def name(self) -> str:
        return "exact_match"

    async def evaluate(
        self, 
        input_text: str, 
        expected_output: str, 
        prediction: str
    ) -> EvaluationResult:
        pred_normalized = prediction.strip().lower()
        expected_normalized = expected_output.strip().lower()
        
        score = 1.0 if pred_normalized == expected_normalized else 0.0
        
        return EvaluationResult(
            example_id="",  # Populated by the runner
            prompt=input_text,
            prediction=prediction,
            expected_output=expected_output,
            score=score,
            evaluator_name=self.name,
            metadata={
                "prediction_len": len(prediction),
                "expected_len": len(expected_output),
                "case_insensitive_match": score == 1.0
            }
        )
