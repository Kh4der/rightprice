"""Django storage backend for a private Vercel Blob store.

Blob objects are never made public.  The application already serves evidence
and generated workbooks through authenticated Django views, so ``open()``
retrieves bytes with the server-side read/write token instead of exposing a
browser-accessible storage URL.
"""

from __future__ import annotations

import mimetypes
import posixpath
from functools import cached_property
from typing import TYPE_CHECKING

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.core.files.utils import validate_file_name
from django.utils.deconstruct import deconstructible
from vercel.blob import BlobClient
from vercel.blob.errors import BlobNotFoundError

if TYPE_CHECKING:
    from django.core.files import File


_DEFAULT_MULTIPART_THRESHOLD = 5 * 1024 * 1024


@deconstructible
class VercelBlobStorage(Storage):
    """Store Django files in a private Vercel Blob store.

    ``BLOB_READ_WRITE_TOKEN`` is passed through Django's ``STORAGES`` setting.
    When ``token`` is omitted, the official SDK also knows how to resolve the
    same environment variable itself.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        multipart_threshold: int = _DEFAULT_MULTIPART_THRESHOLD,
    ) -> None:
        self.token = token or None
        self.multipart_threshold = multipart_threshold

    @cached_property
    def client(self) -> BlobClient:
        return BlobClient(token=self.token)

    @staticmethod
    def _clean_name(name: str) -> str:
        # Blob pathnames are URL paths.  Normalize Windows separators without
        # weakening Django's checks for absolute paths or ``..`` traversal.
        candidate = str(name).replace("\\", "/")
        validate_file_name(candidate, allow_relative_path=True)
        return posixpath.normpath(candidate)

    @staticmethod
    def _clean_directory(path: str) -> str:
        if not path:
            return ""
        return VercelBlobStorage._clean_name(path.rstrip("/"))

    def _open(self, name: str, mode: str = "rb") -> File:
        if any(flag in mode for flag in ("w", "a", "+")):
            raise ValueError("Vercel Blob files are opened read-only.")

        clean_name = self._clean_name(name)
        result = self.client.get(clean_name, access="private")
        if result is None or result.status_code != 200:
            raise FileNotFoundError(clean_name)
        return ContentFile(result.content, name=result.pathname)

    def _save(self, name: str, content: File) -> str:
        clean_name = self._clean_name(name)
        body = b"".join(content.chunks())
        content_type = getattr(content, "content_type", None)
        if not content_type:
            content_type = mimetypes.guess_type(clean_name)[0] or "application/octet-stream"

        uploaded = self.client.put(
            clean_name,
            body,
            access="private",
            content_type=content_type,
            overwrite=False,
            multipart=len(body) >= self.multipart_threshold,
        )
        return uploaded.pathname

    def delete(self, name: str) -> None:
        if name:
            self.client.delete(self._clean_name(name))

    def exists(self, name: str) -> bool:
        try:
            self.client.head(self._clean_name(name))
        except BlobNotFoundError:
            return False
        return True

    def listdir(self, path: str) -> tuple[list[str], list[str]]:
        directory = self._clean_directory(path)
        prefix = f"{directory}/" if directory else ""
        directories: set[str] = set()
        files: set[str] = set()

        for blob in self.client.iter_objects(prefix=prefix or None):
            relative_name = blob.pathname.removeprefix(prefix)
            if not relative_name:
                continue
            head, separator, _tail = relative_name.partition("/")
            (directories if separator else files).add(head)

        return sorted(directories), sorted(files)

    def size(self, name: str) -> int:
        return self.client.head(self._clean_name(name)).size

    def get_created_time(self, name: str):
        return self.client.head(self._clean_name(name)).uploaded_at

    def get_modified_time(self, name: str):
        # Blob objects are immutable in this application, so their upload time
        # is also their last-modified time.
        return self.get_created_time(name)

    def url(self, name: str) -> str:
        self._clean_name(name)
        raise NotImplementedError(
            "Private Vercel Blob files must be served through an authenticated application view."
        )
