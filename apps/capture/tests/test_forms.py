import datetime as dt
import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from django.utils.datastructures import MultiValueDict
from PIL import Image

from apps.capture.forms import DailyReportForm, InventoryCaptureForm, validate_image
from apps.squareapi.client import business_day_for


def image_upload(
    name="photo.jpg",
    *,
    size=(700, 900),
    image_format="JPEG",
    content_type="image/jpeg",
):
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, format=image_format)
    return SimpleUploadedFile(name, output.getvalue(), content_type=content_type)


def test_image_validation_uses_decoded_format_not_browser_content_type():
    upload = image_upload(content_type="text/html")

    validated = validate_image(upload)

    assert validated.content_type == "image/jpeg"
    assert validated.tell() == 0


def test_non_image_disguised_as_jpeg_is_rejected():
    upload = SimpleUploadedFile("fake.jpg", b"<html>not an image</html>", "image/jpeg")

    with pytest.raises(ValidationError, match="not a supported, safe image"):
        validate_image(upload)


def test_too_small_image_is_rejected():
    with pytest.raises(ValidationError, match="too small"):
        validate_image(image_upload(size=(499, 900)))


@override_settings(MAX_UPLOAD_BYTES=100)
def test_upload_byte_limit_is_enforced_before_decoding():
    with pytest.raises(ValidationError, match="too large"):
        validate_image(image_upload())


def test_daily_form_rejects_future_and_stale_business_days():
    today = business_day_for(timezone.now())
    files = {
        "sales_report": image_upload("sales.jpg"),
        "drawer": image_upload("drawer.jpg"),
    }
    future = DailyReportForm(
        {"business_day": today + dt.timedelta(days=1), "note": ""},
        files,
    )
    assert not future.is_valid()
    assert "future" in future.errors["business_day"][0]

    files = {
        "sales_report": image_upload("sales.jpg"),
        "drawer": image_upload("drawer.jpg"),
    }
    stale = DailyReportForm(
        {"business_day": today - dt.timedelta(days=46), "note": ""},
        files,
    )
    assert not stale.is_valid()
    assert "last 45 days" in stale.errors["business_day"][0]


def test_inventory_form_limits_invoice_to_twelve_pages():
    today = business_day_for(timezone.now())
    files = MultiValueDict(
        {"invoice_photos": [image_upload(f"page-{index}.jpg") for index in range(13)]}
    )
    form = InventoryCaptureForm(
        {"business_day": today, "note": "", "vendor_name": "Distributor"},
        files,
    )

    assert not form.is_valid()
    assert "no more than 12" in form.errors["invoice_photos"][0]
