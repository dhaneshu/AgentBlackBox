from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

from .errors import Problem

ALLOWED_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/jsonl",
        "application/octet-stream",
        "application/pdf",
        "text/csv",
        "text/plain",
        "image/jpeg",
        "image/png",
    }
)


@dataclass(frozen=True)
class StoredArtifact:
    key: str
    sha256: str
    mime_type: str
    size: int
    created: bool = True


class ArtifactStore(Protocol):
    def put(self, key: str, data: bytes, mime_type: str) -> StoredArtifact: ...
    def get(self, key: str, expected_sha256: str | None = None) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def signed_url(self, key: str, expires_in: int = 300) -> str: ...


def _validate(data: bytes, mime_type: str) -> str:
    if mime_type not in ALLOWED_MIME_TYPES:
        raise Problem(415, "Unsupported media type", f"{mime_type!r} is not allowed.")
    return hashlib.sha256(data).hexdigest()


class LocalArtifactStore:
    def __init__(
        self,
        root: Path,
        signing_key: bytes,
        previous_signing_keys: tuple[bytes, ...] = (),
    ):
        if len(signing_key) < 32 or any(len(key) < 32 for key in previous_signing_keys):
            raise ValueError("artifact signing keys must contain at least 32 bytes")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._signing_key = signing_key
        self._verification_keys = (signing_key, *previous_signing_keys)

    def _path(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if self.root not in target.parents:
            raise Problem(400, "Invalid artifact key", "Artifact path escapes storage root.")
        return target

    def put(self, key: str, data: bytes, mime_type: str) -> StoredArtifact:
        digest = _validate(data, mime_type)
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_bytes()
            if not hmac.compare_digest(hashlib.sha256(existing).hexdigest(), digest):
                raise Problem(409, "Artifact conflict", "Artifact key has different content.")
            return StoredArtifact(key, digest, mime_type, len(existing), created=False)
        staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}.upload")
        created = True
        try:
            with staging.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(staging, path)
            except FileExistsError:
                existing = path.read_bytes()
                if not hmac.compare_digest(
                    hashlib.sha256(existing).hexdigest(), digest
                ):
                    raise Problem(
                        409, "Artifact conflict", "Artifact key has different content."
                    )
                created = False
        finally:
            staging.unlink(missing_ok=True)
        return StoredArtifact(key, digest, mime_type, len(data), created=created)

    def get(self, key: str, expected_sha256: str | None = None) -> bytes:
        data = self._path(key).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 and not hmac.compare_digest(digest, expected_sha256):
            raise Problem(409, "Artifact digest mismatch", "Stored artifact verification failed.")
        return data

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def signed_url(self, key: str, expires_in: int = 300) -> str:
        expires = int(time.time()) + min(max(expires_in, 30), 3600)
        message = f"{key}:{expires}".encode()
        signature = hmac.new(self._signing_key, message, hashlib.sha256).hexdigest()
        return f"/api/v1/artifact-content/{key}?{urlencode({'expires': expires, 'signature': signature})}"

    def verify_signature(self, key: str, expires: int, signature: str) -> bool:
        if expires < int(time.time()):
            return False
        message = f"{key}:{expires}".encode()
        return any(
            hmac.compare_digest(
                hmac.new(candidate, message, hashlib.sha256).hexdigest(), signature
            )
            for candidate in self._verification_keys
        )


class AzureBlobArtifactStore:
    def __init__(self, account_url: str, container: str, credential=None):
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient, ContentSettings
        except ImportError as exc:
            raise RuntimeError("Install agent-black-box[azure] for Azure Blob support") from exc
        self._content_settings = ContentSettings
        self._credential = credential or DefaultAzureCredential()
        self._service = BlobServiceClient(account_url, credential=self._credential)
        self._container = self._service.get_container_client(container)

    def put(self, key: str, data: bytes, mime_type: str) -> StoredArtifact:
        from azure.core.exceptions import ResourceExistsError

        digest = _validate(data, mime_type)
        created = True
        try:
            self._container.upload_blob(
                key,
                data,
                overwrite=False,
                metadata={"sha256": digest},
                content_settings=self._content_settings(content_type=mime_type),
            )
        except ResourceExistsError:
            self.get(key, digest)
            created = False
        return StoredArtifact(key, digest, mime_type, len(data), created=created)

    def get(self, key: str, expected_sha256: str | None = None) -> bytes:
        blob = self._container.get_blob_client(key)
        data = blob.download_blob(max_concurrency=4).readall()
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 and not hmac.compare_digest(digest, expected_sha256):
            raise Problem(409, "Artifact digest mismatch", "Stored artifact verification failed.")
        return data

    def delete(self, key: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self._container.delete_blob(key, delete_snapshots="include")
        except ResourceNotFoundError:
            pass

    def signed_url(self, key: str, expires_in: int = 300) -> str:
        from datetime import datetime, timedelta, timezone
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        delegation = self._service.get_user_delegation_key(
            datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(hours=1)
        )
        token = generate_blob_sas(
            self._container.account_name,
            self._container.container_name,
            key,
            user_delegation_key=delegation,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(seconds=min(expires_in, 3600)),
        )
        return f"{self._container.url}/{key}?{token}"
