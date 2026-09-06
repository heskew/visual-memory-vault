"""Generate a self-contained multimodal receipt eval dataset.

Renders synthetic receipt images with known ground-truth fields, embeds each as
an inline base64 image part in an EvaluationDataset, and writes the ground truth
into each case's ``reference`` so a metric can grade extracted fields.

Run from the repo root:
    uv run python tests/eval/fixtures/generate_receipt_dataset.py

Deterministic: same input specs -> same dataset. Re-run after editing SPECS.
"""

from __future__ import annotations

import base64
import io
import json
import os

from PIL import Image, ImageDraw, ImageFont

_OUT = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "datasets", "receipts-dataset.json"
)

# Ground truth per receipt. amount is a string to match the agent's RECEIPT line.
SPECS = [
    {
        "id": "receipt_blue_bottle_usd",
        "merchant": "Blue Bottle Coffee",
        "currency": "USD",
        "symbol": "$",
        "date": "2026-03-14",
        "items": [("Latte", "4.50"), ("Cookie", "2.00")],
        "amount": "6.50",
    },
    {
        "id": "receipt_joes_grill_usd",
        "merchant": "Joe's Grill",
        "currency": "USD",
        "symbol": "$",
        "date": "2026-08-20",
        "items": [("Ribeye", "48.00"), ("Sparkling Water", "4.00"), ("Tax", "6.40")],
        "amount": "58.40",
    },
    {
        "id": "receipt_cafe_de_flore_eur",
        "merchant": "Cafe de Flore",
        "currency": "EUR",
        "symbol": "€",
        "date": "2026-05-02",
        "items": [("Croissant", "4.80"), ("Espresso", "3.00"), ("Salade", "16.00")],
        "amount": "23.80",
    },
    {
        "id": "receipt_ace_hardware_usd",
        "merchant": "Ace Hardware",
        "currency": "USD",
        "symbol": "$",
        "date": "2026-01-09",
        "items": [("Drill Bits", "19.99"), ("Paint", "72.10"), ("Tax", "19.98")],
        "amount": "112.07",
    },
]

_PROMPT = (
    "Here is a photo of a receipt. Extract the key details and store it in my "
    "visual memory. In your reply include a short summary and exactly one "
    "machine-readable line of the form:\n"
    'RECEIPT: {"merchant":"...","amount":"...","currency":"...","date":"..."}'
)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/Library/Fonts/Courier New.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_receipt(spec: dict) -> bytes:
    """Render one receipt to JPEG bytes (dark text on white, legible for OCR).

    Names are left-aligned and prices right-aligned by measured width so no
    field (especially the total) is ever clipped.
    """
    width = 600
    pad = 30
    line_h = 36
    ink = (20, 20, 20)
    header = _font(30)
    body = _font(24)

    def price(value: str) -> str:
        return f"{spec['symbol']}{value}"

    # (left_text, right_text_or_None, font, is_rule)
    rows: list[tuple[str, str | None, object, bool]] = [
        (spec["merchant"], None, header, False),
        (spec["date"], None, body, False),
        ("", None, body, True),
    ]
    for name, value in spec["items"]:
        rows.append((name, price(value), body, False))
    rows.append(("", None, body, True))
    rows.append(("TOTAL", price(spec["amount"]), header, False))

    height = pad * 2 + line_h * len(rows)
    img = Image.new("RGB", (width, height), (250, 250, 248))
    draw = ImageDraw.Draw(img)

    y = pad
    for left, right, font, is_rule in rows:
        if is_rule:
            ry = y + line_h // 2
            draw.line([(pad, ry), (width - pad, ry)], fill=(150, 150, 150), width=2)
        else:
            draw.text((pad, y), left, fill=ink, font=font)
            if right is not None:
                rw = draw.textlength(right, font=font)
                draw.text((width - pad - rw, y), right, fill=ink, font=font)
        y += line_h

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue()


def build_case(spec: dict) -> dict:
    b64 = base64.b64encode(render_receipt(spec)).decode("ascii")
    expected = {
        "merchant": spec["merchant"],
        "amount": spec["amount"],
        "currency": spec["currency"],
        "date": spec["date"],
    }
    return {
        "eval_case_id": spec["id"],
        "prompt": {
            "role": "user",
            "parts": [
                {"text": _PROMPT},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            ],
        },
        "reference": {
            "response": {
                "role": "model",
                "parts": [{"text": json.dumps(expected)}],
            }
        },
    }


def main() -> None:
    dataset = {"eval_cases": [build_case(s) for s in SPECS]}
    os.makedirs(os.path.dirname(_OUT), exist_ok=True)
    with open(_OUT, "w") as f:
        json.dump(dataset, f, indent=2)
        f.write("\n")
    kb = os.path.getsize(_OUT) / 1024
    print(f"wrote {_OUT} ({len(dataset['eval_cases'])} cases, {kb:.1f} KB)")


if __name__ == "__main__":
    main()
