"""
Image preparation, tested against privacy-safe synthetic fixtures.

These assert that each step measurably *helps* on images reproducing the relevant
color and glare failure modes, rather than merely asserting that it runs. A
preprocessing step nobody measures is a step that quietly stops working when a
threshold drifts.

Measured on the fixtures (see docs/extraction-notes.md):

* pink lottery stock — the watermark consumes over 20% of the ink-to-paper span
  in greyscale and under 2% in the red channel
* the iPad drawer screen — local contrast roughly doubles, and 0% of the glare
  is clipped to pure white, so nothing under it is lost
"""

import cv2
import numpy as np
import pytest
from django.conf import settings
from PIL import Image, ImageOps

from apps.extraction.preprocess import (
    TARGET_LONG_EDGE,
    local_contrast,
    prepare,
    reduce_glare,
    suppress_pink_watermark,
)

FIXTURES = settings.BASE_DIR / "tests" / "fixtures" / "documents"

PINK = ["fl_lottery_daily_scratchoff_sales.jpg", "fl_lottery_ticket_balance_detail.jpg"]
SCREEN = "square_drawer_screen.jpg"
RECEIPT = "square_sales_report.jpg"


def load(name: str) -> np.ndarray:
    image = ImageOps.exif_transpose(Image.open(FIXTURES / name)).convert("RGB")
    return np.array(image)


def watermark_intrusion(channel: np.ndarray, rgb: np.ndarray) -> float:
    """
    How far the watermark reaches into the ink-to-paper span, 0 to 1.

    0 means the watermark is indistinguishable from clean paper — invisible.
    1 means it is as dark as the printed text, i.e. it competes with the digits.
    """
    rgb = rgb.astype(np.int16)
    grey = cv2.cvtColor(rgb.astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.int16)
    pinkness = rgb[:, :, 0] - rgb[:, :, 1]

    ink = grey < 110
    mark = (pinkness > 45) & ~ink
    paper = (pinkness <= 12) & (grey > 170) & ~ink

    channel = channel.astype(np.int16)
    p, w, i = channel[paper].mean(), channel[mark].mean(), channel[ink].mean()
    return float((p - w) / max(p - i, 1))


# --------------------------------------------------------------------------
# Pink thermal stock
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", PINK)
def test_red_channel_suppresses_the_watermark_better_than_greyscale(name):
    """
    The reason the lottery documents get their own path.

    Greyscale averages the channels, so a magenta watermark darkens the paper
    and sits in among the digits. The watermark is bright in red while the ink is
    dark in every channel, so taking red alone all but removes it.
    """
    rgb = load(name)
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    red = rgb[:, :, 0]

    grey_intrusion = watermark_intrusion(grey, rgb)
    red_intrusion = watermark_intrusion(red, rgb)

    assert red_intrusion < grey_intrusion / 2, (
        f"{name}: red channel left {red_intrusion:.1%} of the ink-to-paper span to the "
        f"watermark against greyscale's {grey_intrusion:.1%}; it should be less than half."
    )
    assert red_intrusion < 0.20


@pytest.mark.parametrize("name", PINK)
def test_watermark_suppression_raises_local_contrast(name):
    rgb = load(name)
    assert local_contrast(suppress_pink_watermark(rgb)) > local_contrast(rgb) * 1.3


@pytest.mark.parametrize("name", PINK)
def test_suppression_preserves_image_dimensions(name):
    """Nothing is cropped away; a cropped digit is an invented figure."""
    rgb = load(name)
    assert suppress_pink_watermark(rgb).shape == rgb.shape


# --------------------------------------------------------------------------
# The glare-streaked screen photo
# --------------------------------------------------------------------------


def test_the_glare_is_not_actually_clipped():
    """
    The premise of not inpainting.

    The streak looks like lost information but almost none of it reaches pure
    white, so the text underneath is still there and can be recovered.
    """
    rgb = load(SCREEN)
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    clipped = float((grey >= 254).mean())

    assert clipped < 0.001, (
        f"{clipped:.4%} of the drawer photo is blown out. If this ever rises, the "
        "flat-field approach stops being sufficient and the glare is genuinely destroying text."
    )


def test_glare_reduction_substantially_raises_local_contrast():
    rgb = load(SCREEN)
    before = local_contrast(rgb)
    after = local_contrast(reduce_glare(rgb))

    assert after > before * 1.8, (
        f"glare reduction moved local contrast {before:.2f} -> {after:.2f}; "
        "the low-contrast grey labels on this screen need better than that."
    )


def test_glare_reduction_preserves_dimensions():
    rgb = load(SCREEN)
    assert reduce_glare(rgb).shape == rgb.shape


# --------------------------------------------------------------------------
# Routing and output
# --------------------------------------------------------------------------


def test_each_document_type_gets_the_right_treatment():
    lottery = prepare((FIXTURES / PINK[0]).read_bytes(), doc_type="LOTTERY_DAILY_SALES")
    screen = prepare((FIXTURES / SCREEN).read_bytes(), doc_type="SQUARE_DRAWER_SCREEN")
    receipt = prepare((FIXTURES / RECEIPT).read_bytes(), doc_type="SQUARE_SALES_REPORT")

    assert "red_channel_watermark_suppression" in lottery.steps
    assert "flat_field_clahe" in screen.steps

    # The white receipt is already high contrast. Every extra operation is a
    # chance to destroy a digit, so it gets none.
    assert "red_channel_watermark_suppression" not in receipt.steps
    assert "flat_field_clahe" not in receipt.steps


def test_orientation_is_always_normalised():
    for name in [*PINK, SCREEN, RECEIPT]:
        result = prepare((FIXTURES / name).read_bytes(), doc_type="SQUARE_SALES_REPORT")
        assert "exif_transpose" in result.steps


def test_output_is_jpeg_within_the_size_budget():
    result = prepare((FIXTURES / RECEIPT).read_bytes(), doc_type="SQUARE_SALES_REPORT")

    assert result.media_type == "image/jpeg"
    assert result.data[:2] == b"\xff\xd8"  # JPEG magic
    assert max(result.width, result.height) <= TARGET_LONG_EDGE


def test_small_images_are_not_upscaled():
    """Upscaling costs patch tokens and adds no detail the model can use."""
    result = prepare((FIXTURES / SCREEN).read_bytes(), doc_type="SQUARE_DRAWER_SCREEN")
    original = load(SCREEN)

    assert result.width <= original.shape[1]
    assert result.height <= original.shape[0]
