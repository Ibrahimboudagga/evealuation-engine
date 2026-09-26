import pytest
import json
from types import SimpleNamespace
from fastapi.testclient import TestClient
from app.api.main import app
from app.providers.factory import ProviderFactory
from app.providers.base import ProviderConfigurationError
from app.providers.openai import OpenAIProvider
from app.providers.anthropic import AnthropicProvider
from app.providers.gemini import GeminiProvider
from app.providers.cohere import CohereProvider

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
async def test_explicit_mock_timeout_supports_credential_free_retry_rehearsal():
    provider = ProviderFactory.create(provider="mock", model_id="mock-timeout")
    assert provider.is_mock is True
    with pytest.raises(TimeoutError, match="pilot rehearsal"):
        await provider.generate("hello")

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


def test_unknown_provider_is_rejected():
    with pytest.raises(ProviderConfigurationError, match="Unknown provider"):
        ProviderFactory.create(provider="not-a-provider", model_id="model", api_key="key")


@pytest.mark.parametrize(
    ("provider_class", "settings_target", "settings"),
    [
        (OpenAIProvider, "app.providers.openai.get_settings", SimpleNamespace(openai_api_key=None)),
        (AnthropicProvider, "app.providers.anthropic.get_settings", SimpleNamespace(anthropic_api_key=None)),
        (GeminiProvider, "app.providers.gemini.get_settings", SimpleNamespace(gemini_api_key=None, google_api_key=None)),
        (CohereProvider, "app.providers.cohere.get_settings", SimpleNamespace(cohere_api_key=None)),
    ],
)
def test_missing_credentials_raise_configuration_error(monkeypatch, provider_class, settings_target, settings):
    monkeypatch.setattr(settings_target, lambda: settings)
    with pytest.raises(ProviderConfigurationError, match="requires an API key"):
        provider_class(model_name="real-model")


def test_compatible_endpoint_can_be_explicitly_unauthenticated(monkeypatch):
    monkeypatch.setattr(
        "app.providers.openai.get_settings",
        lambda: SimpleNamespace(openai_api_key="must-not-be-sent"),
    )
    provider = ProviderFactory.create(
        provider="compatible",
        model_id="local-model",
        base_url="http://localhost:11434/v1",
        allow_unauthenticated=True,
    )
    assert isinstance(provider, OpenAIProvider)
    assert provider.is_mock is False
    assert provider.api_key is None


def test_unauthenticated_access_requires_explicit_compatible_configuration(monkeypatch):
    monkeypatch.setattr(
        "app.providers.openai.get_settings",
        lambda: SimpleNamespace(openai_api_key=None),
    )
    with pytest.raises(ProviderConfigurationError, match="API key"):
        ProviderFactory.create(
            provider="compatible",
            model_id="local-model",
            base_url="http://localhost:11434/v1",
        )
    with pytest.raises(ProviderConfigurationError, match="only supported"):
        ProviderFactory.create(
            provider="groq",
            model_id="model",
            allow_unauthenticated=True,
        )


def test_api_rejects_missing_credential_before_creating_a_run(monkeypatch, tmp_path):
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text('{"input": "hello", "expected_output": "hello"}\n', encoding="utf-8")
    monkeypatch.setattr(
        "app.providers.openai.get_settings",
        lambda: SimpleNamespace(openai_api_key=None),
    )

    with TestClient(app) as client:
        response = client.post(
            "/runs",
            json={
                "dataset_path": str(dataset),
                "candidate_provider": "openai",
                "candidate_model": "gpt-4o",
                "evaluator_provider": "mock",
                "evaluator_model": "mock",
            },
        )

    assert response.status_code == 400
    assert "workspace provider connection" in response.json()["detail"]
