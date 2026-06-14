import structlog
import json
from typing import Optional, Dict, Any, Tuple
from app.providers.base import BaseProvider
from app.config import get_settings

log = structlog.get_logger()

class AnthropicProvider(BaseProvider):
    def __init__(self, model_name: str = "claude-3-5-sonnet-latest", api_key: str = None):
        self.model_name = model_name
        self.api_key = api_key or get_settings().anthropic_api_key
        self.is_mock = not self.api_key or self.api_key == "mock" or self.model_name == "mock"
        
        if self.is_mock:
            log.warning("using_mock_mode", provider="AnthropicProvider", model=self.model_name)
            self.client = None
        else:
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=self.api_key)

    async def generate(self, prompt: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        if self.is_mock:
            if "json" in prompt.lower() or "score" in prompt.lower() or "reason" in prompt.lower():
                return json.dumps({
                    "score": 8,
                    "reason": f"[MOCK Anthropic - {self.model_name}] The prediction is correct and aligns with criteria."
                }), None
            return f"[MOCK Anthropic - {self.model_name}] Response to: {prompt}", None
            
        try:
            response = await self.client.messages.create(
                model=self.model_name,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0
            )
            # Anthropic responses are a list of content blocks
            text = response.content[0].text or ""
            usage = None
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.input_tokens,
                    "completion_tokens": response.usage.output_tokens,
                }
            return text, usage
        except Exception as e:
            log.error("anthropic_generate_failed", error=str(e))
            raise
