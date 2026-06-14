import structlog
from collections import Counter
import math
from app.evaluators.base import BaseEvaluator
from app.schemas.result import EvaluationResult
from app.config import get_settings

log = structlog.get_logger()

# Cache for sentence-transformers model to avoid reloading
_ST_MODEL = None

class SemanticSimilarityEvaluator(BaseEvaluator):
    """
    Evaluator that computes the semantic similarity between predicted and expected text.
    Uses sentence-transformers if available, falling back to a token-based cosine similarity.
    """

    def __init__(self, model_name: str = None):
        self.model_name = model_name or get_settings().similarity_model_name

    @property
    def name(self) -> str:
        return "semantic_similarity"

    def _token_cosine_similarity(self, s1: str, s2: str) -> float:
        """Fallback token-overlap cosine similarity when sentence-transformers is not available."""
        words1 = s1.lower().split()
        words2 = s2.lower().split()
        if not words1 or not words2:
            return 0.0
            
        c1 = Counter(words1)
        c2 = Counter(words2)
        
        # Dot product of counts
        common_words = set(c1.keys()) & set(c2.keys())
        dot_product = sum(c1[word] * c2[word] for word in common_words)
        
        # Magnitudes
        mag1 = math.sqrt(sum(count ** 2 for count in c1.values()))
        mag2 = math.sqrt(sum(count ** 2 for count in c2.values()))
        
        if mag1 == 0 or mag2 == 0:
            return 0.0
            
        return dot_product / (mag1 * mag2)

    async def evaluate(
        self, 
        input_text: str, 
        expected_output: str, 
        prediction: str
    ) -> EvaluationResult:
        global _ST_MODEL
        
        is_fallback = False
        score = 0.0
        reason = ""
        
        try:
            # Attempt to use sentence-transformers
            if _ST_MODEL is None:
                # Lazy import
                from sentence_transformers import SentenceTransformer
                log.info("loading_sentence_transformer_model", model=self.model_name)
                _ST_MODEL = SentenceTransformer(self.model_name)
            
            # Since model.encode is a synchronous, CPU-bound operation, we can run it or import util
            from sentence_transformers import util
            
            embeddings1 = _ST_MODEL.encode(expected_output, convert_to_tensor=True)
            embeddings2 = _ST_MODEL.encode(prediction, convert_to_tensor=True)
            
            cosine_score = util.cos_sim(embeddings1, embeddings2)
            score = float(cosine_score.item())
            
            # Map cosine similarity from [-1, 1] to [0, 1] just in case, though usually positive
            score = max(0.0, min(1.0, score))
            reason = "sentence-transformers embeddings"
            
        except ImportError:
            # Graceful fallback to word-based token cosine similarity
            score = self._token_cosine_similarity(expected_output, prediction)
            is_fallback = True
            reason = "token-overlap cosine similarity (sentence-transformers not installed)"
        except Exception as e:
            # Other errors (e.g. downloading model failed)
            log.warning("sentence_transformer_evaluation_failed_falling_back", error=str(e))
            score = self._token_cosine_similarity(expected_output, prediction)
            is_fallback = True
            reason = f"token-overlap cosine similarity (error loading model: {str(e)})"
            
        return EvaluationResult(
            example_id="",  # Populated by the runner
            prompt=input_text,
            prediction=prediction,
            expected_output=expected_output,
            score=score,
            evaluator_name=self.name,
            metadata={
                "method": reason,
                "is_fallback": is_fallback,
                "model_name": self.model_name if not is_fallback else None
            }
        )
