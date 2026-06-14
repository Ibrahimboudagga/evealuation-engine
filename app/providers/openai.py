import structlog
import json
from typing import Optional, Dict, Any, Tuple
from app.providers.base import BaseProvider
from app.config import get_settings

log = structlog.get_logger()

class OpenAIProvider(BaseProvider):
    def __init__(self, model_name: str = "gpt-4o", api_key: str = None, base_url: str = None):
        self.model_name = model_name
        self.api_key = api_key or get_settings().openai_api_key
        self.base_url = base_url
        self.is_mock = not self.api_key or self.api_key == "mock" or self.model_name == "mock"
        
        if self.is_mock:
            log.warning("using_mock_mode", provider="OpenAIProvider", model=self.model_name)
            self.client = None
        else:
            from openai import AsyncOpenAI
            # Pass base_url if provided for compatible API endpoints (e.g. Groq, HF)
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self.client = AsyncOpenAI(**kwargs)

    async def generate(self, prompt: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        if self.is_mock:
            # If the prompt requests a JSON response (like LLM-as-a-judge), return a valid JSON structure
            if "json" in prompt.lower() or "score" in prompt.lower() or "reason" in prompt.lower():
                return json.dumps({
                    "score": 8,
                    "reason": f"[MOCK OpenAI - {self.model_name}] The prediction is correct and aligns with criteria."
                }), None
            return f"[MOCK OpenAI - {self.model_name}] Response to: {prompt}", None
            
        try:
            response = await self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0
            )
            text = response.choices[0].message.content or ""
            usage = None
            if response.usage:
                usage = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                }
            return text, usage
        except Exception as e:
            log.error("openai_generate_failed", error=str(e))
            raise
