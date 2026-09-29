"""Stack uploaded screenshots into one JPEG.

``POST /capture/stitch`` persists each input and returns 202. This module runs
inside the ingest worker, before extract and ``store_memory``: a vertical
stack, top to bottom, in the order the parts arrived. It does not fetch
remote image URLs.

Source ids are UUID4 strings minted when the request is accepted, one per
file part, in that order. They are not pixel hashes, so two identical inputs
stay distinct. The worker reuses the ids stored on the job when it retries.
Flair ``custom_metadata`` keeps that list as ``source_image_ids`` with
``capture_kind=stitch`` and ``captured_at``.

Caps bound Cloud Run memory. The worker checks headers before it allocates
the canvas, then decodes one source at a time:

- at most ``STITCH_MAX_IMAGES`` inputs
- each input at most ``STITCH_MAX_IMAGE_BYTES`` compressed, ``STITCH_MAX_IMAGE_PIXELS`` pixels, and ``STITCH_MAX_INPUT_EDGE`` px on a side
- the stacked JPEG at most ``STITCH_MAX_OUTPUT_PIXELS`` pixels, ``STITCH_MAX_OUTPUT_WIDTH`` px wide, ``STITCH_MAX_OUTPUT_HEIGHT`` px tall, and ``STITCH_MAX_OUTPUT_BYTES`` encoded

48e6 RGB pixels is about 144MB for the canvas, plus one decoded source.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime

from PIL import Image, UnidentifiedImageError

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass

STITCH_MAX_IMAGES = 8
STITCH_MAX_IMAGE_BYTES = 8 * 1024 * 1024
STITCH_MAX_IMAGE_PIXELS = 16_000_000
STITCH_MAX_INPUT_EDGE = 16384
STITCH_MAX_OUTPUT_PIXELS = 48_000_000
STITCH_MAX_OUTPUT_WIDTH = 16384
STITCH_MAX_OUTPUT_HEIGHT = 65535
STITCH_MAX_OUTPUT_BYTES = 12 * 1024 * 1024
STITCH_JPEG_QUALITY = 85

_PAD = (255, 255, 255)
_HEIF_BRANDS = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"heif", b"mif1", b"msf1"}
)
_MEDIA_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
}


class StitchRejected(Exception):
    """Terminal stitch failure. ``reason`` is stored on the job."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class StitchTransient(Exception):
    """Retryable stitch failure. The job stays pending."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class StitchPart:
    """One accepted input, still the original bytes (not re-encoded)."""

    __slots__ = ("data", "extension", "media_type")

    def __init__(self, data: bytes, media_type: str, extension: str):
        self.data = data
        self.media_type = media_type
        self.extension = extension


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_for_reason(reason: str) -> tuple[int, str]:
    """Status and detail for a stitch rejection raised before a job exists."""
    messages = {
        "too_few_images": (400, "At least 2 images are required"),
        "too_many_images": (
            400,
            f"At most {STITCH_MAX_IMAGES} images are allowed",
        ),
        "empty_image": (400, "Image is empty"),
        "unsupported_image": (
            415,
            "Unsupported image type. Send JPEG, PNG, WebP, or HEIC.",
        ),
        "payload_too_large": (
            413,
            f"Image exceeds {STITCH_MAX_IMAGE_BYTES} bytes",
        ),
        "image_too_large": (
            400,
            f"Image exceeds {STITCH_MAX_IMAGE_PIXELS} pixels or "
            f"{STITCH_MAX_INPUT_EDGE} px on a side",
        ),
        "stitch_too_large": (
            400,
            f"Stitched image would exceed {STITCH_MAX_OUTPUT_PIXELS} pixels, "
            f"{STITCH_MAX_OUTPUT_WIDTH} px wide, or {STITCH_MAX_OUTPUT_HEIGHT} px tall",
        ),
    }
    return messages.get(reason, (400, "Invalid stitch input"))


def sniff_image_type(data: bytes) -> str | None:
    """JPEG, PNG, WebP, or HEIC from magic bytes. Declared Content-Type is ignored."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return "image/heic"
    return None


def _reject_geometry(width: int, height: int, *, output: bool) -> None:
    if width < 1 or height < 1:
        raise StitchRejected("unsupported_image")
    if output:
        if (
            width > STITCH_MAX_OUTPUT_WIDTH
            or height > STITCH_MAX_OUTPUT_HEIGHT
            or width * height > STITCH_MAX_OUTPUT_PIXELS
        ):
            raise StitchRejected("stitch_too_large")
        return
    if (
        width > STITCH_MAX_INPUT_EDGE
        or height > STITCH_MAX_INPUT_EDGE
        or width * height > STITCH_MAX_IMAGE_PIXELS
    ):
        raise StitchRejected("image_too_large")


def _inspect(data: bytes) -> tuple[StitchPart, int, int]:
    """Type and header size. Does not decode pixels into a canvas."""
    if not data:
        raise StitchRejected("empty_image")
    if len(data) > STITCH_MAX_IMAGE_BYTES:
        raise StitchRejected("payload_too_large")
    media_type = sniff_image_type(data)
    if media_type is None:
        raise StitchRejected("unsupported_image")
    try:
        image = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise StitchRejected("image_too_large") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise StitchRejected("unsupported_image") from exc
    except Exception as exc:
        raise StitchRejected("unsupported_image") from exc
    try:
        width, height = image.size
    except Image.DecompressionBombError as exc:
        raise StitchRejected("image_too_large") from exc
    except Exception as exc:
        raise StitchRejected("unsupported_image") from exc
    finally:
        image.close()
    _reject_geometry(width, height, output=False)
    return (
        StitchPart(data, media_type, _MEDIA_EXT[media_type]),
        width,
        height,
    )


def _measure_all(parts: list[bytes]) -> list[tuple[StitchPart, int, int]]:
    if len(parts) < 2:
        raise StitchRejected("too_few_images")
    if len(parts) > STITCH_MAX_IMAGES:
        raise StitchRejected("too_many_images")
    measured = [_inspect(data) for data in parts]
    width = max(item[1] for item in measured)
    height = sum(item[2] for item in measured)
    _reject_geometry(width, height, output=True)
    return measured


def validate_stitch_parts(parts: list[bytes]) -> list[StitchPart]:
    """Accept 2..N images or raise ``StitchRejected`` with a stable reason."""
    return [part for part, _width, _height in _measure_all(parts)]


def assign_source_images(parts: list[StitchPart]) -> list[dict[str, str]]:
    """Mint one UUID4 per input, in list order.

    The id is what Flair stores. ``image_name`` is the durable object key
    ``{id}{extension}`` and is not itself the metadata id.
    """
    sources: list[dict[str, str]] = []
    for part in parts:
        source_id = str(uuid.uuid4())
        sources.append(
            {
                "id": source_id,
                "image_name": f"{source_id}{part.extension}",
                "media_type": part.media_type,
            }
        )
    return sources


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, _PAD)
        background.paste(rgba, mask=rgba.split()[-1])
        if rgba is not image:
            rgba.close()
        return background
    return image.convert("RGB")


def _decode_rgb(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError as exc:
        raise StitchRejected("image_too_large") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise StitchRejected("unsupported_image") from exc
    except Exception as exc:
        raise StitchRejected("unsupported_image") from exc
    try:
        image.load()
        rgb = _to_rgb(image)
    except StitchRejected:
        image.close()
        raise
    except Image.DecompressionBombError as exc:
        image.close()
        raise StitchRejected("image_too_large") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        image.close()
        raise StitchRejected("unsupported_image") from exc
    except Exception as exc:
        image.close()
        raise StitchRejected("unsupported_image") from exc
    if rgb is not image:
        image.close()
    return rgb


def compose_vertical_jpeg(parts: list[bytes]) -> bytes:
    """Stack ``parts`` top to bottom into one JPEG.

    Narrower images are centered on white. Pixels are stacked as stored;
    there is no collage layout and no remote fetch. Raises ``StitchRejected``.
    """
    measured = _measure_all(parts)
    width = max(item[1] for item in measured)
    height = sum(item[2] for item in measured)
    canvas = Image.new("RGB", (width, height), _PAD)
    y = 0
    try:
        for part, part_width, part_height in measured:
            rgb = _decode_rgb(part.data)
            try:
                if rgb.size != (part_width, part_height):
                    raise StitchRejected("unsupported_image")
                x = (width - part_width) // 2
                canvas.paste(rgb, (x, y))
                y += part_height
            finally:
                rgb.close()
        out = io.BytesIO()
        canvas.save(out, format="JPEG", quality=STITCH_JPEG_QUALITY)
        encoded = out.getvalue()
    finally:
        canvas.close()
    if len(encoded) > STITCH_MAX_OUTPUT_BYTES:
        raise StitchRejected("stitch_too_large")
    if not encoded.startswith(b"\xff\xd8"):
        raise StitchRejected("unsupported_image")
    return encoded


def stitch_store_metadata(
    captured_at: str,
    image_url: str,
    source_image_ids: list[str],
) -> dict[str, object]:
    """Flair ``custom_metadata`` for a composed screenshot stack.

    ``source_image_ids`` is the accept-time UUID list in stack order. Non-strings
    are dropped. ``capture_kind`` is always ``stitch``.
    """
    ids = [item for item in source_image_ids if isinstance(item, str) and item]
    return {
        "image_url": image_url,
        "capture_kind": "stitch",
        "source_image_ids": ids,
        "captured_at": captured_at,
    }


def stitch_metadata_from_record(
    record: dict, *, image_url: str = ""
) -> dict[str, object]:
    """Build Flair metadata from a job record or a previous metadata dict."""
    raw_ids = record.get("source_image_ids")
    ids = raw_ids if isinstance(raw_ids, list) else []
    return stitch_store_metadata(
        record.get("captured_at") or "",
        record.get("image_url") or record.get("image_path") or image_url or "",
        ids,
    )
