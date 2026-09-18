from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BLACKBOX_SERVER_", env_file=".env", extra="ignore"
    )

    database_url: str = "sqlite+pysqlite:///:memory:"
    artifact_backend: str = "local"
    artifact_root: Path = Path(".blackbox-artifacts")
    environment: Literal["production", "development", "test"] = "production"
    local_signing_secret: SecretStr | None = None
    local_previous_signing_secrets: list[SecretStr] = Field(default_factory=list)
    azure_blob_account_url: str | None = None
    azure_blob_container: str = "blackbox-artifacts"
    max_event_bytes: int = Field(4_194_304, ge=1024)
    artifact_offload_bytes: int = Field(262_144, ge=1024)
    artifact_url_ttl_seconds: int = Field(300, ge=30, le=3600)
    service_version: str = "0.2.0"
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def secure_database_url(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value.removeprefix("postgres://")
        return value

    @model_validator(mode="after")
    def validate_local_signing(self) -> "ServerSettings":
        secrets = [self.local_signing_secret, *self.local_previous_signing_secrets]
        for secret in (item for item in secrets if item is not None):
            if len(secret.get_secret_value().encode()) < 32:
                raise ValueError("local artifact signing secrets must be at least 32 bytes")
        if (
            self.artifact_backend == "local"
            and self.environment == "production"
            and self.local_signing_secret is None
        ):
            raise ValueError(
                "local_signing_secret is required for local artifacts in production"
            )
        return self
