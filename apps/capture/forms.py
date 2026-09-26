from __future__ import annotations

import io
import uuid
import warnings
from typing import ClassVar

from django import forms
from django.conf import settings
from PIL import Image, UnidentifiedImageError

from apps.squareapi.client import business_day_for

SAFE_IMAGE_MEDIA_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "HEIF": "image/heif",
    "HEIC": "image/heic",
}


class CameraFileInput(forms.ClearableFileInput):
    template_name = "widgets/camera_file.html"

    def __init__(self, attrs=None):
        base = {
            "accept": "image/jpeg,image/png,image/webp,image/heic,image/heif",
            "capture": "environment",
        }
        if attrs:
            base.update(attrs)
        super().__init__(base)


class MultipleCameraInput(CameraFileInput):
    allow_multiple_selected = True


class StagedFileField(forms.FileField):
    """A file field whose required value may arrive as a staged upload ID."""

    staged_required = True


class MultipleImageField(StagedFileField):
    widget = MultipleCameraInput

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else [data]
        return [super().clean(item, initial) for item in files if item]


def validate_image(uploaded):
    if uploaded.size > settings.MAX_UPLOAD_BYTES:
        limit_mb = settings.MAX_UPLOAD_BYTES // (1024 * 1024)
        raise forms.ValidationError(f"Photo is too large. Use a file smaller than {limit_mb} MB.")

    # Import registers the HEIC opener when its platform wheel is present.
    import apps.extraction.preprocess  # noqa: F401

    position = uploaded.tell()
    try:
        raw = uploaded.read()
        Image.MAX_IMAGE_PIXELS = settings.MAX_IMAGE_PIXELS
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(raw))
            image.verify()
            width, height = image.size
            image_format = (image.format or "").upper()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise forms.ValidationError("This file is not a supported, safe image.") from exc
    finally:
        uploaded.seek(position)

    if image_format not in SAFE_IMAGE_MEDIA_TYPES:
        raise forms.ValidationError("Use a JPEG, PNG, WebP, or HEIC photo.")
    if min(width, height) < 500:
        raise forms.ValidationError(
            "Photo is too small. Retake it closer so the numbers are readable."
        )
    if width * height > settings.MAX_IMAGE_PIXELS:
        raise forms.ValidationError(
            "Photo has too many pixels. Use the phone's standard camera size."
        )
    # UploadedFile.content_type is supplied by the browser and is not trustworthy.
    # document_file reflects this value in an inline response, so persisting an
    # attacker-provided value such as text/html would create a same-origin content
    # injection surface. Pillow's decoded format is the source of truth here.
    uploaded.content_type = SAFE_IMAGE_MEDIA_TYPES[image_format]
    return uploaded


class BaseCaptureForm(forms.Form):
    staged_limits: ClassVar[dict[str, int]] = {}

    staged_uploads = forms.JSONField(required=False, widget=forms.HiddenInput)
    business_day = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(
        label="Note for the owner",
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            from django.utils import timezone

            self.initial.setdefault("business_day", business_day_for(timezone.now()))

    def clean_business_day(self):
        from django.utils import timezone

        day = self.cleaned_data["business_day"]
        today = business_day_for(timezone.now())
        if day > today:
            raise forms.ValidationError("Business day cannot be in the future.")
        if (today - day).days > 45:
            raise forms.ValidationError("Choose a business day from the last 45 days.")
        return day

    def clean_staged_uploads(self):
        """Parse only the shape here; ownership and one-time use are locked later."""

        value = self.cleaned_data.get("staged_uploads") or {}
        if not isinstance(value, dict):
            raise forms.ValidationError("The staged photo list is invalid. Choose the photos again.")

        normalized: dict[str, list[str]] = {}
        seen: set[uuid.UUID] = set()
        for field_name, raw_ids in value.items():
            if field_name not in self.staged_limits or not isinstance(raw_ids, list):
                raise forms.ValidationError(
                    "The staged photo list is invalid. Choose the photos again."
                )
            if len(raw_ids) > self.staged_limits[field_name]:
                raise forms.ValidationError(
                    f"Too many photos were staged for {field_name.replace('_', ' ')}."
                )
            normalized_ids = []
            for raw_id in raw_ids:
                try:
                    pending_id = uuid.UUID(str(raw_id))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise forms.ValidationError(
                        "The staged photo list is invalid. Choose the photos again."
                    ) from exc
                if pending_id in seen:
                    raise forms.ValidationError("A staged photo cannot be used more than once.")
                seen.add(pending_id)
                normalized_ids.append(str(pending_id))
            if normalized_ids:
                normalized[field_name] = normalized_ids
        return normalized

    def staged_count(self, field_name: str) -> int:
        staged = self.cleaned_data.get("staged_uploads")
        if not isinstance(staged, dict):
            return 0
        return len(staged.get(field_name, []))


class DailyReportForm(BaseCaptureForm):
    staged_limits: ClassVar[dict[str, int]] = {
        "sales_report": 1,
        "drawer": 1,
        "lottery_daily": 1,
        "ticket_balance": 1,
    }

    sales_report = StagedFileField(
        label="Square sales report",
        required=False,
        validators=[validate_image],
        widget=CameraFileInput(),
        help_text="Photograph the full printout on a plain background.",
    )
    drawer = StagedFileField(
        label="Ended drawer screen",
        required=False,
        validators=[validate_image],
        widget=CameraFileInput(),
        help_text="End the drawer first. Include counted cash and over/short.",
    )
    lottery_daily = forms.FileField(
        label="Lottery daily sales",
        required=False,
        validators=[validate_image],
        widget=CameraFileInput(),
    )
    ticket_balance = forms.FileField(
        label="Lottery ticket balance",
        required=False,
        validators=[validate_image],
        widget=CameraFileInput(),
    )

    def clean(self):
        cleaned = super().clean()
        for field_name in ("sales_report", "drawer"):
            if not cleaned.get(field_name) and not self.staged_count(field_name):
                self.add_error(field_name, "Add this required photo.")
        for field_name, limit in self.staged_limits.items():
            uploaded = 1 if cleaned.get(field_name) else 0
            if uploaded + self.staged_count(field_name) > limit:
                self.add_error(field_name, "Choose only one photo for this slot.")
        return cleaned


class PayoutCaptureForm(BaseCaptureForm):
    staged_limits: ClassVar[dict[str, int]] = {"payout_photo": 1}

    payout_photo = StagedFileField(
        label="Winning-ticket or payout evidence",
        required=False,
        validators=[validate_image],
        widget=CameraFileInput(),
    )
    amount = forms.DecimalField(
        label="Amount paid (optional)",
        required=False,
        min_value=0,
        max_digits=10,
        decimal_places=2,
        help_text="Enter it now if known; the photo will still be read and checked.",
    )

    def clean(self):
        cleaned = super().clean()
        total = (1 if cleaned.get("payout_photo") else 0) + self.staged_count("payout_photo")
        if total == 0:
            self.add_error("payout_photo", "Add the payout evidence photo.")
        elif total > 1:
            self.add_error("payout_photo", "Choose only one payout photo.")
        return cleaned


class InventoryCaptureForm(BaseCaptureForm):
    staged_limits: ClassVar[dict[str, int]] = {"invoice_photos": 12}

    invoice_photos = MultipleImageField(
        label="Delivery invoice photo(s)",
        required=False,
        validators=[],
        widget=MultipleCameraInput(attrs={"data-max-files": "12"}),
        help_text="Add every page, one document per photo.",
    )
    vendor_name = forms.CharField(label="Distributor", required=False, max_length=160)

    def clean_invoice_photos(self):
        files = self.cleaned_data.get("invoice_photos", [])
        return [validate_image(item) for item in files]

    def clean(self):
        cleaned = super().clean()
        files = cleaned.get("invoice_photos", [])
        total = len(files) + self.staged_count("invoice_photos")
        if total == 0:
            self.add_error("invoice_photos", "Add at least one invoice photo.")
        elif total > 12:
            self.add_error("invoice_photos", "Upload no more than 12 invoice pages at once.")
        return cleaned
