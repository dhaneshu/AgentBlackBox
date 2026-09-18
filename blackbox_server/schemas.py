from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ResourceCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"name": "evaluation-platform", "metadata": {}}]}
    )
    name: str = Field(min_length=1, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResourceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    workspace_id: str
    name: str
    metadata_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    version: int


class Page(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class EventCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "event_id": "01JEVENT000000000000000000",
                    "type": "run.status",
                    "run_id": "01JRUN00000000000000000000",
                    "payload": {"status": "succeeded"},
                }
            ]
        }
    )
    event_id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=100)
    run_id: str | None = None
    payload: dict[str, Any]


class JobCreate(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"type": "evaluation", "payload": {"run_id": "01JRUN"}}]
        }
    )
    type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(5, ge=1, le=25)


class RetentionCreate(BaseModel):
    resource_type: str = Field(min_length=1, max_length=100)
    retain_days: int = Field(ge=1, le=36500)
    enabled: bool = True


class DatasetVersionCreate(BaseModel):
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    document: dict[str, Any]
