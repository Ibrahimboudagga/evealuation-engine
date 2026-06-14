import structlog
from typing import Optional
from app.providers.base import BaseProvider
from app.providers.openai import OpenAIProvider
from app.providers.anthropic import AnthropicProvider
from app.providers.gemini import GeminiProvider
from app.providers.cohere import CohereProvider

log = structlog.get_logger()

class ProviderFactory:
    """
    Factory class to instantiate LLM providers based on provider identifier,
    model ID, and optional authentication configurations.
    """
    
    @staticmethod
    def create(
        provider: str,
        model_id: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None
    ) -> BaseProvider:
        """
        Creates and returns a provider instance.
        
        Args:
            provider: The provider identifier (e.g. 'openai', 'anthropic', 'gemini', 'groq', 'huggingface')
            model_id: The specific model ID (e.g. 'gpt-4o', 'claude-3-5-sonnet-latest')
            api_key: Custom API key override.
            base_url: Custom API endpoint URL (for compatible platforms like Groq/Hugging Face).
            
        Returns:
            An instance of BaseProvider.
        """
        provider_clean = provider.lower().strip()
        
        # Backward compatibility check: if model_id is not provided, split provider (e.g. "openai-gpt-4o" or "openai-mock")
        if model_id is None:
            if "-" in provider_clean:
                parts = provider_clean.split("-", 1)
                provider_clean = parts[0]
                model_id = parts[1]
            else:
                model_id = provider_clean
                
        # Handle explicitly requested mock keys or mock models
        is_force_mock = api_key == "mock" or model_id == "mock"
        if is_force_mock:
            api_key = "mock"

        if provider_clean == "openai":
            return OpenAIProvider(model_name=model_id, api_key=api_key, base_url=base_url)
            
        elif provider_clean == "anthropic":
            return AnthropicProvider(model_name=model_id, api_key=api_key)
            
        elif provider_clean in ("gemini", "google"):
            # Support friendly short names
            if model_id == "flash":
                model_id = "gemini-1.5-flash"
            elif model_id == "pro":
                model_id = "gemini-1.5-pro"
            return GeminiProvider(model_name=model_id, api_key=api_key)

        elif provider_clean in ("cohere", "co"):
            return CohereProvider(model_name=model_id, api_key=api_key, base_url=base_url)

        elif provider_clean in ("groq", "huggingface", "hf", "openai-compatible", "compatible"):
            # API-compatible provider using OpenAIProvider configured with a base_url
            actual_base_url = base_url
            if provider_clean == "groq" and not actual_base_url:
                actual_base_url = "https://api.groq.com/openai/v1"
            return OpenAIProvider(model_name=model_id, api_key=api_key, base_url=actual_base_url)
            
        elif provider_clean == "mock" or provider_clean == "dummy":
            return OpenAIProvider(model_name="mock-model", api_key="mock")
            
        else:
            log.warning(
                "unknown_provider_defaulting_to_openai",
                provider=provider,
                model_id=model_id,
            )
            return OpenAIProvider(model_name=model_id, api_key=api_key, base_url=base_url)
