from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import Header


@dataclass(frozen=True)
class RequestIdentity:
    subject: str
    workspace_id: str | None = None


class IdentityProvider(Protocol):
    def resolve(self, subject: str, workspace_id: str | None) -> RequestIdentity: ...


class PlaceholderIdentityProvider:
    """Phase 6 seam only; Phase 7 replaces this with verified OIDC identity."""

    def resolve(self, subject: str, workspace_id: str | None) -> RequestIdentity:
        return RequestIdentity(subject=subject, workspace_id=workspace_id)


def request_identity(
    x_blackbox_subject: str = Header("anonymous"),
    x_workspace_id: str | None = Header(None),
) -> RequestIdentity:
    return PlaceholderIdentityProvider().resolve(x_blackbox_subject, x_workspace_id)
