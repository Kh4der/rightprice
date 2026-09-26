from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from vercel.blob.errors import BlobNotFoundError

from apps.core.storage import VercelBlobStorage


@pytest.fixture
def storage():
    backend = VercelBlobStorage(token="test-blob-token")
    backend.__dict__["client"] = Mock()
    return backend


def test_save_writes_a_private_immutable_blob_with_detected_content_type(storage):
    storage.client.head.side_effect = BlobNotFoundError()
    storage.client.put.return_value = SimpleNamespace(pathname="submissions/one/evidence.jpg")

    saved_name = storage.save(
        "submissions\\one\\evidence.jpg",
        ContentFile(b"photo bytes", name="evidence.jpg"),
    )

    assert saved_name == "submissions/one/evidence.jpg"
    storage.client.put.assert_called_once_with(
        "submissions/one/evidence.jpg",
        b"photo bytes",
        access="private",
        content_type="image/jpeg",
        overwrite=False,
        multipart=False,
    )


def test_save_uses_multipart_for_large_files(storage):
    storage.client.head.side_effect = BlobNotFoundError()
    storage.client.put.return_value = SimpleNamespace(pathname="large.bin")
    storage.multipart_threshold = 4

    storage.save("large.bin", ContentFile(b"1234"))

    assert storage.client.put.call_args.kwargs["multipart"] is True


def test_open_downloads_private_content_server_side(storage):
    storage.client.get.return_value = SimpleNamespace(
        status_code=200,
        content=b"corrected workbook",
        pathname="inventory/job/corrected.xlsx",
    )

    opened = storage.open("inventory/job/corrected.xlsx", "rb")

    assert opened.read() == b"corrected workbook"
    storage.client.get.assert_called_once_with(
        "inventory/job/corrected.xlsx",
        access="private",
    )


@pytest.mark.parametrize("result", [None, SimpleNamespace(status_code=304)])
def test_open_raises_file_not_found_for_a_missing_blob(storage, result):
    storage.client.get.return_value = result

    with pytest.raises(FileNotFoundError):
        storage.open("missing.jpg", "rb")


def test_exists_only_converts_not_found_to_false(storage):
    storage.client.head.side_effect = BlobNotFoundError()
    assert storage.exists("missing.jpg") is False

    storage.client.head.side_effect = RuntimeError("storage unavailable")
    with pytest.raises(RuntimeError, match="storage unavailable"):
        storage.exists("error.jpg")


def test_metadata_delete_and_directory_listing(storage):
    uploaded_at = datetime(2026, 9, 26, 12, tzinfo=UTC)
    storage.client.head.return_value = SimpleNamespace(size=42, uploaded_at=uploaded_at)
    storage.client.iter_objects.return_value = iter(
        [
            SimpleNamespace(pathname="submissions/one/a.jpg"),
            SimpleNamespace(pathname="submissions/one/nested/b.jpg"),
            SimpleNamespace(pathname="submissions/one/nested/c.jpg"),
        ]
    )

    assert storage.size("submissions/one/a.jpg") == 42
    assert storage.get_created_time("submissions/one/a.jpg") == uploaded_at
    assert storage.get_modified_time("submissions/one/a.jpg") == uploaded_at
    assert storage.listdir("submissions/one") == (["nested"], ["a.jpg"])

    storage.delete("submissions/one/a.jpg")
    storage.client.delete.assert_called_once_with("submissions/one/a.jpg")


def test_private_storage_has_no_direct_browser_url(storage):
    with pytest.raises(NotImplementedError, match="authenticated application view"):
        storage.url("submissions/one/evidence.jpg")


@pytest.mark.parametrize("name", ["../secret", "/absolute/path", "safe/../../secret"])
def test_blob_names_reject_path_traversal_and_absolute_paths(storage, name):
    with pytest.raises(SuspiciousFileOperation):
        storage.exists(name)
