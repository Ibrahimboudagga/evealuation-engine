from abc import ABC, abstractmethod
from app.schemas.result import EvaluationResult

class BaseEvaluator(ABC):
    """
    Abstract base class for all evaluation metrics.
    """
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Returns the unique name of the evaluator."""
        pass

    @abstractmethod
    async def evaluate(
        self, 
        input_text: str, 
        expected_output: str, 
        prediction: str
    ) -> EvaluationResult:
        """
        Evaluates the prediction against the expected output.
        
        Args:
            input_text: The input prompt/text.
            expected_output: The expected output/ground truth.
            prediction: The actual output/prediction from the model.
            
        Returns:
            An EvaluationResult instance containing the score and metadata.
        """
        pass
