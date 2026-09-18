from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass
class Problem(Exception):
    status: int
    title: str
    detail: str
    type: str = "about:blank"
    extensions: dict[str, Any] | None = None


def problem_response(request: Request, exc: Problem) -> JSONResponse:
    body: dict[str, Any] = {
        "type": exc.type,
        "title": exc.title,
        "status": exc.status,
        "detail": exc.detail,
        "instance": str(request.url.path),
        "request_id": getattr(request.state, "request_id", None),
    }
    if exc.extensions:
        body.update(exc.extensions)
    return JSONResponse(body, exc.status, media_type="application/problem+json")
