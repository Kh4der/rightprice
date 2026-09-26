"""
Per-document image preparation.

There is no single pipeline here, because the four documents fail in three
different ways and the fix for one actively harms another. The transforms were
selected from the original private examples and are continuously measured on
privacy-safe synthetic fixtures — see `docs/extraction-notes.md` and the tests
in `apps/extraction/tests/`.

The three cases:

* **Pink lottery thermal stock.** The Florida Lottery flamingo watermark is
  magenta; the text is near-black. Taking the RED CHANNEL alone all but erases
  the watermark, because magenta is bright in red while black is dark in every
  channel. Greyscale, which averages the channels, keeps roughly a fifth of the
  watermark and leaves the digits sitting in it.

* **The iPad drawer screen.** A specular streak across the glass. Inpainting it
  is the obvious move and it is WRONG: the streak overlaps real text, so
  inpainting deletes characters — it erased "Started by" outright in testing.
  The glare is additive and barely clipped, so the text survives underneath it;
  flat-field division plus CLAHE recovers it without removing anything.

* **The white Square receipt.** Already high contrast. Left alone beyond
  orientation and resize, because every extra operation is a chance to destroy
  something.

Nothing here crops, masks or inpaints. A step that can delete a digit is not
worth a step that might make one easier to read.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageOps

# Registered so HEIC straight off an iPhone opens like any other image.
try:  # pragma: no cover - depends on the platform wheel
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pass

# Large source photos increase vision latency and cost after receipt digits are
# already comfortably legible. A 1600px long edge preserves the tested sample
# detail while keeping the provider boundary predictable. Production acceptance
# still has to benchmark this setting against the store's real invoice layouts.
TARGET_LONG_EDGE = 1600
JPEG_QUALITY = 90


@dataclass(frozen=True)
class Prepared:
    """The bytes to send, plus what was done to get them."""

    data: bytes
    media_type: str
    width: int
    height: int
    steps: tuple[str, ...]


def prepare(raw: bytes, *, doc_type: str, rotation_degrees: int = 0) -> Prepared:
    """Open, orient, resize and clean one photograph for its document type."""
    steps: list[str] = []

    if rotation_degrees not in {0, 90, 180, 270}:
        raise ValueError("rotation_degrees must be one of 0, 90, 180 or 270")

    image = Image.open(io.BytesIO(raw))
    # The classic sideways-photo bug: phones record orientation in EXIF rather
    # than rotating the pixels.
    image = ImageOps.exif_transpose(image)
    steps.append("exif_transpose")

    if rotation_degrees:
        # PIL rotates counter-clockwise, while the classifier reports the
        # clockwise correction a person would apply to the photograph.
        image = image.rotate(-rotation_degrees, expand=True)
        steps.append(f"rotate_clockwise_{rotation_degrees}")

    if image.mode != "RGB":
        image = image.convert("RGB")
        steps.append("to_rgb")

    image = _resize(image)
    steps.append(f"resize_long_edge_{TARGET_LONG_EDGE}")

    array = np.array(image)

    if _is_pink_thermal(doc_type):
        array = suppress_pink_watermark(array)
        steps.append("red_channel_watermark_suppression")
    elif _is_screen_photo(doc_type):
        array = reduce_glare(array)
        steps.append("flat_field_clahe")

    out = Image.fromarray(array)
    buffer = io.BytesIO()
    out.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)

    return Prepared(
        data=buffer.getvalue(),
        media_type="image/jpeg",
        width=out.width,
        height=out.height,
        steps=tuple(steps),
    )


# --------------------------------------------------------------------------
# The two real interventions
# --------------------------------------------------------------------------


def suppress_pink_watermark(rgb: np.ndarray) -> np.ndarray:
    """
    Drop everything but the red channel.

    The watermark is magenta, so it reads near-white in red and vanishes. The
    ink is near-black in every channel, so it survives. A colour-distance mask
    achieves the same thing with far more code and a threshold to tune.
    """
    red = rgb[:, :, 0]
    # Mild CLAHE afterwards: red-channel paper is bright and flat, and this
    # lifts the thermal print off it without crushing thin strokes.
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(red)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2RGB)


def reduce_glare(rgb: np.ndarray) -> np.ndarray:
    """
    Even out a specular streak without removing anything.

    Divide by a heavily blurred copy of the image to estimate and cancel the
    illumination field, then CLAHE for local contrast. Because the glare is
    additive and only a negligible fraction of it is clipped to pure white, the
    text underneath is still present and comes back.

    Deliberately NOT inpainting. The streak crosses real characters, and
    inpainting replaces them with plausible background.
    """
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    # Large kernel: we want the illumination gradient, not the text.
    background = cv2.GaussianBlur(grey, (0, 0), sigmaX=51)
    background = np.maximum(background, 1)

    flattened = (grey.astype(np.float32) / background.astype(np.float32)) * 128.0
    flattened = np.clip(flattened, 0, 255).astype(np.uint8)

    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(flattened)
    return cv2.cvtColor(enhanced, cv2.COLOR_GRAY2RGB)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _resize(image: Image.Image) -> Image.Image:
    long_edge = max(image.width, image.height)
    if long_edge <= TARGET_LONG_EDGE:
        return image
    scale = TARGET_LONG_EDGE / long_edge
    return image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)


def _is_pink_thermal(doc_type: str) -> bool:
    return doc_type in {"LOTTERY_DAILY_SALES", "LOTTERY_TICKET_BALANCE", "LOTTERY_DRAW_SCHEDULE"}


def _is_screen_photo(doc_type: str) -> bool:
    return doc_type == "SQUARE_DRAWER_SCREEN"


def local_contrast(rgb: np.ndarray) -> float:
    """
    Mean local standard deviation — how much the text stands off the paper.

    Used by the tests to assert a preprocessing step actually helped, rather
    than assuming it did.
    """
    grey = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    mean = cv2.blur(grey, (15, 15))
    sq = cv2.blur(grey * grey, (15, 15))
    return float(np.sqrt(np.maximum(sq - mean * mean, 0)).mean())
