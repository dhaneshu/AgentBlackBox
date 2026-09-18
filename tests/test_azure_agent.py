"""Azure agent configuration and client wiring without network access."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from blackbox.agents import REGISTRY
from blackbox.azure_agent import AzureGroundedAgent


def _response(text: str = "READY"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3),
    )


def test_azure_agent_is_selectable_from_the_cli():
    assert REGISTRY["azure-grounded"] is AzureGroundedAgent


def test_complete_uses_entra_and_omits_optional_temperature(monkeypatch):
    captured = {}

    class FakeAzureOpenAI:
        def __init__(self, **options):
            captured["options"] = options
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create)
            )

        @staticmethod
        def create(**request):
            captured["request"] = request
            return _response()

    openai = ModuleType("openai")
    openai.AzureOpenAI = FakeAzureOpenAI
    identity = ModuleType("azure.identity")
    identity.DefaultAzureCredential = lambda **options: ("credential", options)
    identity.get_bearer_token_provider = lambda credential, scope: (
        credential, scope
    )
    azure = ModuleType("azure")
    azure.identity = identity
    monkeypatch.setitem(sys.modules, "openai", openai)
    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("OPENAI_DEPLOYMENT", "gpt-5-mini")
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-id")
    monkeypatch.delenv("AZURE_OPENAI_AUTH", raising=False)

    assert AzureGroundedAgent()._complete("hello") == ("READY", 11, 3)
    assert "azure_ad_token_provider" in captured["options"]
    assert "api_key" not in captured["options"]
    assert "temperature" not in captured["request"]
    credential, _scope = captured["options"]["azure_ad_token_provider"]
    assert credential[1]["visual_studio_code_tenant_id"] == "tenant-id"
    assert credential[1]["broker_tenant_id"] == "tenant-id"


def test_api_key_auth_requires_a_key(monkeypatch):
    openai = ModuleType("openai")
    openai.AzureOpenAI = object
    monkeypatch.setitem(sys.modules, "openai", openai)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("OPENAI_DEPLOYMENT", "deployment")
    monkeypatch.setenv("AZURE_OPENAI_AUTH", "api_key")
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="AZURE_OPENAI_API_KEY"):
        AzureGroundedAgent()._complete("hello")
