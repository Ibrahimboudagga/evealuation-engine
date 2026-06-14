import pytest
import json
from app.providers.factory import ProviderFactory
from app.providers.openai import OpenAIProvider
from app.providers.anthropic import AnthropicProvider
from app.providers.gemini import GeminiProvider

def test_provider_factory_resolution():
    openai_mock = ProviderFactory.create("openai-mock")
    assert isinstance(openai_mock, OpenAIProvider)
    assert openai_mock.is_mock is True
    
    anthropic_mock = ProviderFactory.create("anthropic-mock")
    assert isinstance(anthropic_mock, AnthropicProvider)
    assert anthropic_mock.is_mock is True
    
    gemini_mock = ProviderFactory.create("gemini-mock")
    assert isinstance(gemini_mock, GeminiProvider)
    assert gemini_mock.is_mock is True

@pytest.mark.asyncio
async def test_mock_generation_text():
    provider = ProviderFactory.create("openai-mock")
    response, usage = await provider.generate("Who are you?")
    assert "MOCK OpenAI" in response
    assert "Who are you?" in response
    assert usage is None

@pytest.mark.asyncio
async def test_mock_generation_json():
    provider = ProviderFactory.create("openai-mock")
    response, usage = await provider.generate("Return a json response with score and reason.")
    
    # Verify it is valid JSON
    data = json.loads(response)
    assert "score" in data
    assert "reason" in data
    assert isinstance(data["score"], int)
    assert isinstance(data["reason"], str)
    assert usage is None

def test_provider_factory_agnostic_creation():
    # Test creation using provider and model_id arguments explicitly
    provider = ProviderFactory.create(provider="openai", model_id="gpt-3.5-turbo", api_key="mock")
    assert isinstance(provider, OpenAIProvider)
    assert provider.model_name == "gpt-3.5-turbo"
    assert provider.is_mock is True

    # Test Groq resolution (uses OpenAIProvider under the hood with a custom base_url)
    groq_provider = ProviderFactory.create(provider="groq", model_id="llama3-8b", api_key="mock")
    assert isinstance(groq_provider, OpenAIProvider)
    assert groq_provider.model_name == "llama3-8b"
    assert groq_provider.base_url == "https://api.groq.com/openai/v1"

    # Test custom base_url override
    custom_provider = ProviderFactory.create(
        provider="huggingface", 
        model_id="custom-model", 
        api_key="mock", 
        base_url="https://api-inference.huggingface.co/models/custom-model"
    )
    assert isinstance(custom_provider, OpenAIProvider)
    assert custom_provider.model_name == "custom-model"
    assert custom_provider.base_url == "https://api-inference.huggingface.co/models/custom-model"
