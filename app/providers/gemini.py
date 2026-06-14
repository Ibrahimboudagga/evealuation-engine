import structlog
import json
from typing import Optional, Dict, Any, Tuple
from app.providers.base import BaseProvider
from app.config import get_settings

log = structlog.get_logger()

class GeminiProvider(BaseProvider):
    def __init__(self, model_name: str = "gemini-1.5-flash", api_key: str = None):
        self.model_name = model_name
        settings = get_settings()
        # google-generativeai client library uses GEMINI_API_KEY or GOOGLE_API_KEY
        self.api_key = api_key or settings.gemini_api_key or settings.google_api_key
        self.is_mock = not self.api_key or self.api_key == "mock" or self.model_name == "mock"
        
        if self.is_mock:
            log.warning("using_mock_mode", provider="GeminiProvider", model=self.model_name)
            self.model = None
        else:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel(model_name)

    async def generate(self, prompt: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        if self.is_mock:
            if "json" in prompt.lower() or "score" in prompt.lower() or "reason" in prompt.lower():
                return json.dumps({
                    "score": 9,
                    "reason": f"[MOCK Gemini - {self.model_name}] The prediction is correct and aligns with criteria."
                }), None
            return f"[MOCK Gemini - {self.model_name}] Response to: {prompt}", None
            
        try:
            response = await self.model.generate_content_async(
                contents=prompt,
                generation_config={"temperature": 0.0}
            )
            text = response.text or ""
            usage = None
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                usage = {
                    "prompt_tokens": getattr(response.usage_metadata, "prompt_token_count", None),
                    "completion_tokens": getattr(response.usage_metadata, "candidates_token_count", None),
                }
            return text, usage
        except Exception as e:
            log.error("gemini_generate_failed", error=str(e))
            raise
