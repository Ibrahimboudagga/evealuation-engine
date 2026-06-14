from typing import Dict, List, Optional
from app.evaluators.base import BaseEvaluator
from app.evaluators.exact_match import ExactMatchEvaluator
from app.evaluators.similarity import SemanticSimilarityEvaluator
from app.evaluators.llm_judge import LLMAsAJudgeEvaluator
from app.providers.base import BaseProvider

class EvaluatorRegistry:
    """
    Registry for managing evaluation metrics. 
    Allows clean retrieval and dynamic registration of new evaluators.
    """
    
    def __init__(self, judge_provider: Optional[BaseProvider] = None, judge_prompt_template: Optional[str] = None):
        self._evaluators: Dict[str, BaseEvaluator] = {}
        
        # Register default standard evaluators
        self.register(ExactMatchEvaluator())
        self.register(SemanticSimilarityEvaluator())
        
        # Register LLM-as-a-judge if a provider is configured
        if judge_provider:
            self.register(LLMAsAJudgeEvaluator(judge_provider, prompt_template=judge_prompt_template))

    def register(self, evaluator: BaseEvaluator) -> None:
        """Registers a new evaluator instance."""
        self._evaluators[evaluator.name] = evaluator

    def get(self, name: str) -> BaseEvaluator:
        """Retrieves an evaluator by its name identifier."""
        if name not in self._evaluators:
            raise ValueError(f"Evaluator '{name}' is not registered.")
        return self._evaluators[name]

    def get_all(self) -> List[BaseEvaluator]:
        """Returns all registered evaluators."""
        return list(self._evaluators.values())
