"""Typed configuration loaded from environment variables and ``.env``."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """Raised when configuration cannot safely be used."""


class _Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
    )

    def public_dict(self) -> dict[str, object]:
        """Serialize configuration while always excluding secret fields."""
        return self.model_dump(exclude={"api_key", "access_token"}, mode="json")


class LocalProfile(_Settings):
    """Offline execution profile."""

    kind: Literal["local"] = "local"
    corpus: Path = Path("samples/corpus")
    suite: Path = Path("samples/suites/northwind.jsonl")
    output: Path = Path("out")


class RemoteProfile(_Settings):
    """Remote service profile."""

    model_config = SettingsConfigDict(
        env_prefix="BLACKBOX_REMOTE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kind: Literal["remote"] = "remote"
    url: str
    workspace_id: str
    project_id: str
    access_token: SecretStr | None = Field(default=None, exclude=True)


class AzureOpenAISettings(_Settings):
    """Azure OpenAI settings preserving the existing environment contract."""

    endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AZURE_OPENAI_ENDPOINT", "endpoint"),
    )
    deployment: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_DEPLOYMENT", "deployment"),
    )
    auth: Literal["entra", "api_key"] = Field(
        default="entra",
        validation_alias=AliasChoices("AZURE_OPENAI_AUTH", "auth"),
    )
    api_version: str = Field(
        default="2024-10-21",
        validation_alias=AliasChoices("AZURE_OPENAI_API_VERSION", "api_version"),
    )
    tenant_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AZURE_TENANT_ID", "tenant_id"),
    )
    api_key: SecretStr | None = Field(
        default=None,
        exclude=True,
        validation_alias=AliasChoices("AZURE_OPENAI_API_KEY", "api_key"),
    )

    def require_runtime(self, deployment_override: str | None = None) -> str:
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", self.endpoint),
                ("OPENAI_DEPLOYMENT", deployment_override or self.deployment),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError(
                "Missing environment variable(s): " + ", ".join(missing)
            )
        if self.auth == "api_key" and self.api_key is None:
            raise ConfigurationError(
                "Missing environment variable: AZURE_OPENAI_API_KEY"
            )
        return deployment_override or self.deployment or ""


def load_azure_openai_settings() -> AzureOpenAISettings:
    """Load Azure settings and normalize Pydantic errors for CLI consumers."""
    try:
        return AzureOpenAISettings()
    except ValidationError as exc:
        raise ConfigurationError(f"Invalid Azure OpenAI configuration: {exc}") from exc
