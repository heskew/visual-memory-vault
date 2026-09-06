import base64
import io
import json
import pathlib

import pytest
from PIL import Image

_DATASET = (
    pathlib.Path(__file__).resolve().parents[1]
    / "eval"
    / "datasets"
    / "receipts-dataset.json"
)


def _cases():
    data = json.loads(_DATASET.read_text())
    return data["eval_cases"]


def test_dataset_has_cases():
    assert len(_cases()) >= 3


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["eval_case_id"])
def test_case_is_well_formed_multimodal(case):
    parts = case["prompt"]["parts"]
    texts = [p for p in parts if "text" in p]
    images = [p for p in parts if "inline_data" in p]
    assert texts, "case must include a text instruction part"
    assert len(images) == 1, "case must include exactly one inline image"

    inline = images[0]["inline_data"]
    assert inline["mime_type"] == "image/jpeg"
    raw = base64.b64decode(inline["data"])
    img = Image.open(io.BytesIO(raw))
    img.verify()  # valid JPEG

    ref = json.loads(case["reference"]["response"]["parts"][0]["text"])
    assert set(ref) == {"merchant", "amount", "currency", "date"}
    assert all(ref[k] for k in ref)
