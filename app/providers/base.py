from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Tuple

class BaseProvider(ABC):
    """
    Abstract base class for all LLM providers.
    """
    @abstractmethod
    async def generate(self, prompt: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Generates a text completion for the given prompt.
        
        Args:
            prompt: The input prompt.
            
        Returns:
            A tuple of (generated response string, optional usage dict with
            'prompt_tokens' and 'completion_tokens' keys).
        """
        pass
