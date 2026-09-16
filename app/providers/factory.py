import structlog
from typing import Optional
from app.providers.base import BaseProvider, ProviderConfigurationError
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
        base_url: Optional[str] = None,
        allow_unauthenticated: bool = False,
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
                
        supported_providers = {
            "openai", "anthropic", "gemini", "google", "cohere", "co",
            "groq", "huggingface", "hf", "openai-compatible", "compatible",
            "mock", "dummy", "demo",
        }
        if provider_clean not in supported_providers:
            raise ProviderConfigurationError(
                f"Unknown provider '{provider}'. Choose a supported provider or explicit 'mock' demo mode."
            )

        # Demo behavior is only enabled by an explicit mock selection.
        is_demo = provider_clean in {"mock", "dummy", "demo"} or model_id == "mock" or api_key == "mock"
        if is_demo:
            demo_base_url = base_url
            if provider_clean == "groq" and not demo_base_url:
                demo_base_url = "https://api.groq.com/openai/v1"
            if provider_clean == "anthropic":
                return AnthropicProvider(model_name=model_id or "mock", api_key="mock", demo_mode=True)
            if provider_clean in {"gemini", "google"}:
                return GeminiProvider(model_name=model_id or "mock", api_key="mock", demo_mode=True)
            if provider_clean in {"cohere", "co"}:
                return CohereProvider(
                    model_name=model_id or "mock", api_key="mock", base_url=demo_base_url, demo_mode=True
                )
            return OpenAIProvider(
                model_name=model_id or "mock",
                api_key="mock",
                base_url=demo_base_url,
                demo_mode=True,
            )

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
            supports_unauthenticated = provider_clean in {"openai-compatible", "compatible"}
            if allow_unauthenticated and not supports_unauthenticated:
                raise ProviderConfigurationError(
                    "Unauthenticated access is only supported for the explicit "
                    "'compatible' or 'openai-compatible' provider selection."
                )
            if allow_unauthenticated and not actual_base_url:
                raise ProviderConfigurationError(
                    "An unauthenticated compatible endpoint requires base_url."
                )
            return OpenAIProvider(
                model_name=model_id,
                api_key=api_key,
                base_url=actual_base_url,
                allow_unauthenticated=allow_unauthenticated,
                use_default_api_key=not allow_unauthenticated,
            )
