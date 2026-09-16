import structlog
import json
from typing import Optional, Dict, Any, Tuple
from app.providers.base import BaseProvider, ProviderConfigurationError
from app.config import get_settings

log = structlog.get_logger()


class CohereProvider(BaseProvider):
    def __init__(self, model_name: str = "command-r-plus", api_key: str = None, base_url: str = None, demo_mode: bool = False):
        self.model_name = model_name
        self.api_key = api_key or get_settings().cohere_api_key
        self.base_url = base_url
        self.is_mock = demo_mode or self.api_key == "mock" or self.model_name == "mock"

        if self.is_mock:
            log.warning("using_mock_mode", provider="CohereProvider", model=self.model_name)
            self.client = None
        else:
            if not self.api_key:
                raise ProviderConfigurationError(
                    "Cohere requires an API key. Set COHERE_API_KEY or pass api_key; "
                    "select mock explicitly for demo mode."
                )
            from cohere import AsyncClient
            kwargs: Dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self.client = AsyncClient(**kwargs)

    async def generate(self, prompt: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        if self.is_mock:
            if "json" in prompt.lower() or "score" in prompt.lower() or "reason" in prompt.lower():
                return json.dumps({
                    "score": 8,
                    "reason": f"[MOCK Cohere - {self.model_name}] The prediction is correct and aligns with criteria."
                }), None
            return f"[MOCK Cohere - {self.model_name}] Response to: {prompt}", None

        try:
            response = await self.client.chat(model=self.model_name, message=prompt)
            text = response.text or ""
            usage = None
            if hasattr(response, "meta") and response.meta and hasattr(response.meta, "tokens") and response.meta.tokens:
                usage = {
                    "prompt_tokens": getattr(response.meta.tokens, "input_tokens", None),
                    "completion_tokens": getattr(response.meta.tokens, "output_tokens", None),
                }
            return text, usage
        except Exception as e:
            log.error("cohere_generate_failed", error=str(e))
            raise
