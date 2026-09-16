from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Tuple


class ProviderConfigurationError(ValueError):
    """Raised when a provider cannot be used safely with the supplied settings."""


class BaseProvider(ABC):
    """
    Abstract base class for all LLM providers.
    """
    is_mock: bool = False

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
