"""Stitch compose: vertical JPEG, 202 contract, caps, and worker outcomes."""

from __future__ import annotations

import asyncio
import io
import json
import struct
import uuid
import zlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from frontend import stitch as stitch_mod
from frontend.main import (
    PersistError,
    _upload_ingest_prompt,
    load_job,
    load_stitch_media_bytes,
    load_uploaded_image_bytes,
)
from frontend.stitch import (
    STITCH_MAX_IMAGE_BYTES,
    STITCH_MAX_IMAGE_PIXELS,
    STITCH_MAX_IMAGES,
    STITCH_MAX_INPUT_EDGE,
    STITCH_MAX_OUTPUT_BYTES,
    STITCH_MAX_OUTPUT_HEIGHT,
    STITCH_MAX_OUTPUT_PIXELS,
    STITCH_MAX_OUTPUT_WIDTH,
    StitchRejected,
    StitchTransient,
    assign_source_images,
    compose_vertical_jpeg,
    sniff_image_type,
    stitch_metadata_from_record,
    stitch_store_metadata,
    validate_stitch_parts,
)

_COMPOSE = stitch_mod.compose_vertical_jpeg


def _solid(
    width: int, height: int, color: tuple[int, int, int], fmt: str = "JPEG"
) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format=fmt)
    return buf.getvalue()


def _png_header(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


def _near(
    pixel: tuple[int, ...], expected: tuple[int, int, int], tol: int = 40
) -> None:
    assert all(
        abs(channel - want) <= tol
        for channel, want in zip(pixel, expected, strict=False)
    ), (
        pixel,
        expected,
    )


@pytest.fixture
def stitch_env(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", None)
    monkeypatch.setattr("frontend.main.INGEST_DRAIN_INTERVAL_SEC", 0)
    monkeypatch.setattr("frontend.main.CLOUD_TASKS_QUEUE", None)
    monkeypatch.setattr("frontend.main.API_KEY", "")
    monkeypatch.setattr("frontend.main.ALLOW_UNAUTHENTICATED", True)
    return tmp_path


def test_caps_match_the_cloud_run_budget():
    assert STITCH_MAX_IMAGES == 8
    assert STITCH_MAX_IMAGE_BYTES == 8 * 1024 * 1024
    assert STITCH_MAX_IMAGE_PIXELS == 16_000_000
    assert STITCH_MAX_INPUT_EDGE == 16384
    assert STITCH_MAX_OUTPUT_PIXELS == 48_000_000
    assert STITCH_MAX_OUTPUT_WIDTH == 16384
    assert STITCH_MAX_OUTPUT_HEIGHT == 65535
    assert STITCH_MAX_OUTPUT_BYTES == 12 * 1024 * 1024


def test_stitch_module_does_not_fetch_urls():
    text = Path("frontend/stitch.py").read_text().lower()
    for banned in (
        "httpx",
        "urlopen",
        "requests",
        "socket",
        "capture_public_page",
        "transcribe",
    ):
        assert banned not in text


def test_readme_documents_stitch_product():
    readme = Path("README.md").read_text()
    onboarding = Path("docs/ONBOARDING.md").read_text()
    for text in (readme, onboarding):
        assert "/capture/stitch" in text
        assert "source_image_ids" in text
        assert "capture_kind" in text
    assert "16000000" in readme
    assert "48000000" in readme
    assert "65535" in readme
    assert "8 MiB" in readme
    assert "12 MiB" in readme
    assert "At most 8 images" in readme
    assert "Remote image URLs are not fetched" in readme
    assert "not hashes of the pixels" in readme


def test_sniff_accepts_upload_types_and_rejects_gif():
    assert sniff_image_type(_solid(4, 4, (1, 2, 3))) == "image/jpeg"
    assert sniff_image_type(_solid(4, 4, (1, 2, 3), "PNG")) == "image/png"
    assert sniff_image_type(_solid(4, 4, (1, 2, 3), "WEBP")) == "image/webp"
    heic = _solid(4, 4, (9, 9, 9), "HEIF")
    assert sniff_image_type(heic) == "image/heic"
    assert sniff_image_type(b"GIF89a" + b"\x00" * 16) is None
    assert sniff_image_type(b"<html></html>") is None
    assert sniff_image_type(b"") is None


def test_metadata_shape_and_prompt_copy_stitch_fields():
    metadata = stitch_store_metadata(
        "2026-09-28T12:00:00Z",
        "/media/stack_stitch.jpg",
        ["id-a", "id-b", 3, ""],
    )
    assert metadata == {
        "image_url": "/media/stack_stitch.jpg",
        "capture_kind": "stitch",
        "source_image_ids": ["id-a", "id-b"],
        "captured_at": "2026-09-28T12:00:00Z",
    }
    assert "source_url" not in metadata
    again = stitch_metadata_from_record(metadata)
    assert again == metadata
    prompt = _upload_ingest_prompt(
        "stitch.jpg", "/media/stack_stitch.jpg", "Trip", None, metadata
    )
    assert json.dumps(metadata) in prompt
    assert "source_image_ids" in prompt
    assert "capture_kind" in prompt
    assert "source_url" not in prompt
    assert "Trip" in prompt
    assert "Stitched screenshots" not in prompt
    bare = _upload_ingest_prompt(
        "stitch.jpg", "/media/stack_stitch.jpg", None, None, metadata
    )
    assert "Stitched screenshots" in bare
    upload = _upload_ingest_prompt("shot.jpg", "/media/shot.jpg", "Dinner")
    assert "source_image_ids" not in upload
    assert "capture_kind" not in upload


def test_source_ids_are_distinct_uuids_in_order():
    same = _solid(4, 4, (10, 20, 30))
    parts = validate_stitch_parts([same, same])
    sources = assign_source_images(parts)
    assert sources[0]["id"] != sources[1]["id"]
    for source in sources:
        parsed = uuid.UUID(source["id"])
        assert parsed.version == 4
        assert source["image_name"] == f"{source['id']}.jpg"
        assert source["media_type"] == "image/jpeg"
    assert [source["id"] for source in sources] == [
        sources[0]["id"],
        sources[1]["id"],
    ]


def test_vertical_stack_preserves_order_and_centers_narrow_images():
    red = _solid(16, 16, (220, 10, 10))
    green = _solid(8, 16, (10, 220, 10), "PNG")
    blue = _solid(16, 16, (10, 10, 220), "WEBP")
    encoded = compose_vertical_jpeg([red, green, blue])
    assert encoded.startswith(b"\xff\xd8")
    with Image.open(io.BytesIO(encoded)) as stacked:
        assert stacked.size == (16, 48)
        assert stacked.mode == "RGB"
        _near(stacked.getpixel((8, 8)), (220, 10, 10))
        _near(stacked.getpixel((8, 24)), (10, 220, 10))
        _near(stacked.getpixel((0, 24)), (255, 255, 255), tol=8)
        _near(stacked.getpixel((8, 40)), (10, 10, 220))


def test_heic_inputs_stack():
    encoded = compose_vertical_jpeg(
        [
            _solid(6, 4, (200, 0, 0), "HEIF"),
            _solid(6, 4, (0, 0, 200), "HEIF"),
        ]
    )
    with Image.open(io.BytesIO(encoded)) as stacked:
        assert stacked.size == (6, 8)
        _near(stacked.getpixel((3, 2)), (200, 0, 0))
        _near(stacked.getpixel((3, 6)), (0, 0, 200))


def test_validate_rejects_count_type_and_geometry():
    jpeg = _solid(4, 4, (1, 1, 1))
    with pytest.raises(StitchRejected) as exc:
        validate_stitch_parts([jpeg])
    assert exc.value.reason == "too_few_images"
    with pytest.raises(StitchRejected) as exc:
        validate_stitch_parts([jpeg] * (STITCH_MAX_IMAGES + 1))
    assert exc.value.reason == "too_many_images"
    with pytest.raises(StitchRejected) as exc:
        validate_stitch_parts([jpeg, b""])
    assert exc.value.reason == "empty_image"
    with pytest.raises(StitchRejected) as exc:
        validate_stitch_parts([jpeg, b"GIF89a" + b"\x00" * 20])
    assert exc.value.reason == "unsupported_image"
    with pytest.raises(StitchRejected) as exc:
        validate_stitch_parts([_png_header(10000, 2000), jpeg])
    assert exc.value.reason == "image_too_large"
    with pytest.raises(StitchRejected) as exc:
        validate_stitch_parts([_png_header(20000, 20000), jpeg])
    assert exc.value.reason == "image_too_large"
    with pytest.raises(StitchRejected) as exc:
        validate_stitch_parts([_png_header(17000, 10), jpeg])
    assert exc.value.reason == "image_too_large"


def test_output_pixel_cap_rejects_before_the_canvas(monkeypatch):
    monkeypatch.setattr(stitch_mod, "STITCH_MAX_OUTPUT_PIXELS", 10)
    allocated: list[tuple[int, int]] = []
    real_new = stitch_mod.Image.new

    def track_canvas(mode, size, *args, **kwargs):
        if mode == "RGB" and size == (4, 8):
            allocated.append(size)
        return real_new(mode, size, *args, **kwargs)

    monkeypatch.setattr(stitch_mod.Image, "new", track_canvas)
    with pytest.raises(StitchRejected) as exc:
        compose_vertical_jpeg([_solid(4, 4, (1, 1, 1)), _solid(4, 4, (2, 2, 2))])
    assert exc.value.reason == "stitch_too_large"
    assert allocated == []


def test_encoded_output_over_the_byte_cap_is_terminal(monkeypatch):
    monkeypatch.setattr(stitch_mod, "STITCH_MAX_OUTPUT_BYTES", 20)
    with pytest.raises(StitchRejected) as exc:
        compose_vertical_jpeg([_solid(8, 8, (3, 3, 3)), _solid(8, 8, (4, 4, 4))])
    assert exc.value.reason == "stitch_too_large"


def _parts(*blobs: bytes, subject: str | None = None, extra: dict | None = None):
    files = [
        ("file", (f"shot-{index}.jpg", blob, "image/jpeg"))
        for index, blob in enumerate(blobs)
    ]
    data = dict(extra or {})
    if subject is not None:
        data["subject"] = subject
    return files, data


@pytest.mark.asyncio
async def test_stitch_202_does_not_compose_and_mints_stable_ids(
    stitch_env, monkeypatch
):
    calls = {"n": 0}

    def counting(parts):
        calls["n"] += 1
        return _COMPOSE(parts)

    monkeypatch.setattr(stitch_mod, "compose_vertical_jpeg", counting)
    scheduled: list[str] = []
    monkeypatch.setattr("frontend.main.CLOUD_TASKS_QUEUE", "vault-ingest")
    monkeypatch.setattr(
        "frontend.main.create_ingest_cloud_task",
        lambda job_id: scheduled.append(job_id),
    )
    red = _solid(8, 8, (200, 0, 0))
    blue = _solid(8, 8, (0, 0, 200), "PNG")
    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        started = asyncio.get_running_loop().time()
        response = await client.post(
            "/capture/stitch?wait=1",
            files=[
                ("file", ("top.jpg", red, "image/jpeg")),
                ("images", ("bottom.png", blue, "image/png")),
            ],
            data={"subject": "  Trip notes  ", "url": "http://169.254.169.254/"},
        )
        elapsed = asyncio.get_running_loop().time() - started
    assert response.status_code == 202
    assert elapsed < 2
    assert calls["n"] == 0
    body = response.json()
    assert set(body) == {"status", "job_id", "image_path"}
    assert body["status"] == "accepted"
    assert body["image_path"].endswith("_stitch.jpg")
    assert "summary" not in body
    job_id = body["job_id"]
    assert scheduled == [job_id]
    assert uuid.UUID(job_id).version == 4
    record = json.loads((stitch_env / "jobs" / f"{job_id}.json").read_text())
    assert record["status"] == "pending"
    assert record["capture_kind"] == "stitch"
    assert record["subject"] == "Trip notes"
    assert record["capture_rendered"] is False
    assert record["captured_at"] is None
    assert "source_url" not in record
    assert record["image_path"] == body["image_path"]
    assert not (stitch_env / body["image_path"].rsplit("/", 1)[-1]).exists()
    ids = record["source_image_ids"]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert [item["id"] for item in record["source_images"]] == ids
    assert (stitch_env / record["source_images"][0]["image_name"]).read_bytes() == red
    assert (stitch_env / record["source_images"][1]["image_name"]).read_bytes() == blue
    assert record["source_images"][1]["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_stitch_polls_pending_then_succeeded_with_flair_metadata(
    stitch_env, monkeypatch
):
    calls = {"n": 0}
    real = stitch_mod.compose_vertical_jpeg

    def counting(parts):
        calls["n"] += 1
        return real(parts)

    monkeypatch.setattr(stitch_mod, "compose_vertical_jpeg", counting)
    seen = {}

    async def fake_ingest(*args, stitch=None, **kwargs):
        seen["stitch"] = stitch
        seen["filename"] = args[1]
        seen["media_type"] = args[2]
        seen["prompt"] = _upload_ingest_prompt(args[1], args[3], args[4], None, stitch)
        return "Stored the composed screenshots"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", fake_ingest)
    red = _solid(16, 16, (220, 10, 10))
    green = _solid(8, 16, (10, 200, 10), "PNG")
    blue = _solid(16, 16, (10, 10, 220))
    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/capture/stitch",
            files=[
                ("file", ("a.jpg", red, "image/jpeg")),
                ("file", ("b.png", green, "image/png")),
                ("file", ("c.jpg", blue, "text/plain")),
            ],
            data={"subject": "Trip"},
        )
        assert accepted.status_code == 202
        assert calls["n"] == 0
        job_id = accepted.json()["job_id"]
        image_path = accepted.json()["image_path"]
        pending = await client.get(f"/jobs/{job_id}")
        assert pending.status_code == 200
        assert pending.json() == {
            "job_id": job_id,
            "status": "pending",
            "image_path": image_path,
        }
        missing = await client.get(f"/media/{image_path.rsplit('/', 1)[-1]}")
        assert missing.status_code == 404
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        consumer = await client.post("/ingest", json={"job_id": job_id})
        assert consumer.status_code == 200
        assert consumer.json()["completed"] == [job_id]
        done = await client.get(f"/jobs/{job_id}")
        assert done.json()["status"] == "succeeded"
        assert done.json()["summary"] == "Stored the composed screenshots"
        media = await client.get(f"/media/{image_path.rsplit('/', 1)[-1]}")
        assert media.status_code == 200
        assert media.headers["content-type"].startswith("image/jpeg")

    assert calls["n"] == 1
    with Image.open(io.BytesIO(media.content)) as stacked:
        assert stacked.size == (16, 48)
        _near(stacked.getpixel((8, 8)), (220, 10, 10))
        _near(stacked.getpixel((8, 24)), (10, 200, 10))
        _near(stacked.getpixel((8, 40)), (10, 10, 220))
    stored = seen["stitch"]
    record = load_job(job_id)
    assert stored["capture_kind"] == "stitch"
    assert stored["source_image_ids"] == record["source_image_ids"]
    assert stored["image_url"] == image_path
    assert stored["captured_at"].endswith("Z")
    assert record["captured_at"] == stored["captured_at"]
    assert record["capture_rendered"] is True
    assert seen["filename"] == "stitch.jpg"
    assert seen["media_type"] == "image/jpeg"
    assert json.dumps(stored) in seen["prompt"]
    assert "composed stack of screenshots" in seen["prompt"]


@pytest.mark.asyncio
async def test_stitch_retries_keep_source_ids_and_skip_recompose(
    stitch_env, monkeypatch
):
    import httpx
    from a2a.client import A2AClientError

    calls = {"n": 0}
    real = stitch_mod.compose_vertical_jpeg

    def counting(parts):
        calls["n"] += 1
        return real(parts)

    monkeypatch.setattr(stitch_mod, "compose_vertical_jpeg", counting)
    attempts = {"n": 0}

    async def flaky(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            request = httpx.Request("POST", "http://127.0.0.1/a2a")
            response = httpx.Response(503, request=request)
            http_err = httpx.HTTPStatusError("a2a", request=request, response=response)
            exc = A2AClientError("HTTP Error 503: a2a")
            exc.__cause__ = http_err
            raise exc
        return "Stored on retry"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", flaky)
    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post(
            "/capture/stitch",
            files=_parts(_solid(4, 4, (1, 2, 3)), _solid(4, 4, (4, 5, 6)))[0],
        )
        job_id = accepted.json()["job_id"]
        ids = load_job(job_id)["source_image_ids"]
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        first = await client.post("/ingest", json={"job_id": job_id})
        assert first.status_code == 503
        assert load_job(job_id)["status"] == "pending"
        assert load_job(job_id)["source_image_ids"] == ids
        assert calls["n"] == 1
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        second = await client.post("/ingest", json={"job_id": job_id})
        assert second.status_code == 200
        assert second.json()["completed"] == [job_id]
    assert calls["n"] == 1
    assert load_job(job_id)["source_image_ids"] == ids
    assert load_job(job_id)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_stitch_rejects_and_terminal_failures(stitch_env, monkeypatch):
    from frontend.main import app

    transport = ASGITransport(app=app)
    jpeg = _solid(4, 4, (8, 8, 8))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        none = await client.post(
            "/capture/stitch",
            files={"subject": (None, "only")},
        )
        assert none.status_code == 400
        assert none.json()["detail"] == "At least 2 images are required"
        one = await client.post("/capture/stitch", files=_parts(jpeg)[0])
        assert one.status_code == 400
        assert one.json()["detail"] == "At least 2 images are required"
        too_many = await client.post(
            "/capture/stitch",
            files=_parts(*([jpeg] * (STITCH_MAX_IMAGES + 1)))[0],
        )
        assert too_many.status_code == 400
        assert (
            too_many.json()["detail"]
            == f"At most {STITCH_MAX_IMAGES} images are allowed"
        )
        gif = await client.post(
            "/capture/stitch",
            files=[
                ("file", ("a.jpg", jpeg, "image/jpeg")),
                ("file", ("b.jpg", b"GIF89a" + b"\x00" * 24, "image/jpeg")),
            ],
        )
        assert gif.status_code == 415
        assert "JPEG, PNG, WebP, or HEIC" in gif.json()["detail"]
        huge = await client.post(
            "/capture/stitch",
            files=[
                ("file", ("a.png", _png_header(10000, 2000), "image/png")),
                ("file", ("b.jpg", jpeg, "image/jpeg")),
            ],
        )
        assert huge.status_code == 400
        assert "16000000" in huge.json()["detail"]
        remote = await client.post(
            "/capture/stitch",
            json={"url": "http://169.254.169.254/latest"},
        )
        assert remote.status_code == 400
        assert "multipart" in remote.json()["detail"]
        blank = await client.post(
            "/capture/stitch",
            files=_parts(jpeg, jpeg)[0],
            data={"subject": "   "},
        )
        assert blank.status_code == 202
        assert load_job(blank.json()["job_id"])["subject"] is None
        long_subject = "S" * 600
        trimmed = await client.post(
            "/capture/stitch",
            files=_parts(jpeg, jpeg, subject=long_subject)[0],
            data={"subject": long_subject},
        )
        assert trimmed.status_code == 202
        assert load_job(trimmed.json()["job_id"])["subject"] == "S" * 500

        monkeypatch.setattr(stitch_mod, "STITCH_MAX_IMAGE_BYTES", 32)
        oversized = await client.post(
            "/capture/stitch",
            files=_parts(jpeg, jpeg)[0],
        )
        assert oversized.status_code == 413
        assert oversized.json()["detail"] == "Image exceeds 32 bytes"
        monkeypatch.setattr(
            stitch_mod, "STITCH_MAX_IMAGE_BYTES", STITCH_MAX_IMAGE_BYTES
        )

        accepted = await client.post("/capture/stitch", files=_parts(jpeg, jpeg)[0])
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        record = load_job(job_id)
        source_name = record["source_images"][0]["image_name"]
        (stitch_env / source_name).write_bytes(b"this is not an image")
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        failed = await client.post("/ingest", json={"job_id": job_id})
        assert failed.status_code == 200
        assert failed.json()["completed"] == [job_id]
        body = (await client.get(f"/jobs/{job_id}")).json()
        assert body["status"] == "failed"
        assert body["error"] == "unsupported_image"
        again = await client.post("/ingest", json={"job_id": job_id})
        assert again.status_code == 200
        assert (await client.get(f"/jobs/{job_id}")).json()[
            "error"
        ] == "unsupported_image"

        missing = await client.post("/capture/stitch", files=_parts(jpeg, jpeg)[0])
        missing_id = missing.json()["job_id"]
        gone = load_job(missing_id)["source_images"][1]["image_name"]
        (stitch_env / gone).unlink()
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        await client.post("/ingest", json={"job_id": missing_id})
        assert (await client.get(f"/jobs/{missing_id}")).json()["error"] == (
            "stitch_source_missing"
        )


@pytest.mark.asyncio
async def test_stitch_persist_and_transient_errors_stay_pending(
    stitch_env, monkeypatch
):
    from frontend.main import app, persist_uploaded_image

    real_persist = persist_uploaded_image
    real_compose = stitch_mod.compose_vertical_jpeg

    def fail_output(data, name, media):
        if name.endswith("_stitch.jpg"):
            raise PersistError("gcs blip")
        return real_persist(data, name, media)

    monkeypatch.setattr("frontend.main.persist_uploaded_image", fail_output)
    jpeg = _solid(4, 4, (7, 7, 7))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post("/capture/stitch", files=_parts(jpeg, jpeg)[0])
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        ids = load_job(job_id)["source_image_ids"]
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        first = await client.post("/ingest", json={"job_id": job_id})
        assert first.status_code == 503
        assert load_job(job_id)["status"] == "pending"
        assert load_job(job_id)["source_image_ids"] == ids
        assert load_job(job_id)["error"] is None

        monkeypatch.setattr("frontend.main.persist_uploaded_image", real_persist)
        monkeypatch.setattr(
            stitch_mod,
            "compose_vertical_jpeg",
            lambda _parts: (_ for _ in ()).throw(StitchTransient("stitch_unavailable")),
        )
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        second = await client.post("/ingest", json={"job_id": job_id})
        assert second.status_code == 503
        assert load_job(job_id)["status"] == "pending"
        assert load_job(job_id)["source_image_ids"] == ids

        monkeypatch.setattr(stitch_mod, "compose_vertical_jpeg", real_compose)

        async def succeed(*_args, **_kwargs):
            return "Stored after retry"

        monkeypatch.setattr("frontend.main.ingest_uploaded_image", succeed)
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        done = await client.post("/ingest", json={"job_id": job_id})
        assert done.status_code == 200
        assert (await client.get(f"/jobs/{job_id}")).json()["status"] == "succeeded"
        assert load_job(job_id)["source_image_ids"] == ids


@pytest.mark.asyncio
async def test_upload_and_url_contracts_stay_unchanged(stitch_env):
    from frontend.main import app

    jpeg = _solid(4, 4, (1, 1, 1))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upload = await client.post(
            "/upload",
            files={"file": ("receipt.jpg", jpeg, "image/jpeg")},
            data={"subject": "Dinner"},
        )
        assert upload.status_code == 202
        assert set(upload.json()) == {"status", "job_id", "image_path"}
        upload_job = load_job(upload.json()["job_id"])
        assert "capture_kind" not in upload_job
        assert "source_image_ids" not in upload_job
        assert upload_job["subject"] == "Dinner"

        captured = await client.post(
            "/capture/url",
            json={"url": "https://example.com/docs", "subject": "Docs"},
        )
        assert captured.status_code == 202
        assert captured.json()["image_path"].endswith("_page.jpg")
        url_job = load_job(captured.json()["job_id"])
        assert url_job["capture_kind"] == "url"
        assert url_job["source_url"] == "https://example.com/docs"
        assert "source_image_ids" not in url_job


@pytest.mark.asyncio
async def test_stitch_requires_api_key_when_configured(stitch_env, monkeypatch):
    monkeypatch.setattr("frontend.main.API_KEY", "ci-secret")
    monkeypatch.setattr("frontend.main.ALLOW_UNAUTHENTICATED", False)
    from frontend.main import app

    jpeg = _solid(4, 4, (1, 1, 1))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post("/capture/stitch", files=_parts(jpeg, jpeg)[0])
        assert denied.status_code == 401
        allowed = await client.post(
            "/capture/stitch",
            files=_parts(jpeg, jpeg)[0],
            headers={"X-Api-Key": "ci-secret"},
        )
        assert allowed.status_code == 202


def test_encoded_cap_constant_is_twelve_mib():
    assert STITCH_MAX_OUTPUT_BYTES == 12 * 1024 * 1024
    assert STITCH_MAX_OUTPUT_HEIGHT == 65535


class _GcsBlob:
    def __init__(self, name: str):
        self.name = name
        self._data: bytes | str | None = None
        self.generation = 0
        self.fail_exists = False
        self.fail_download = False
        self.exists_calls = 0
        self.download_calls = 0

    def exists(self) -> bool:
        self.exists_calls += 1
        if self.fail_exists:
            raise RuntimeError("gcs blip")
        return self._data is not None

    def reload(self) -> None:
        return None

    def download_as_text(self) -> str:
        if isinstance(self._data, bytes):
            return self._data.decode()
        return self._data or ""

    def download_as_bytes(self) -> bytes:
        self.download_calls += 1
        if self.fail_download:
            raise RuntimeError("gcs blip")
        if self._data is None:
            raise RuntimeError("missing")
        if isinstance(self._data, str):
            return self._data.encode()
        return self._data

    def upload_from_string(
        self,
        payload: bytes | str,
        content_type: str | None = None,
        if_generation_match: int | None = None,
    ) -> None:
        current_gen = self.generation if self._data is not None else 0
        if if_generation_match is not None and current_gen != if_generation_match:
            from google.api_core.exceptions import PreconditionFailed

            raise PreconditionFailed("generation mismatch")
        self._data = payload
        self.generation = current_gen + 1


class _GcsBucket:
    def __init__(self) -> None:
        self.blobs: dict[str, _GcsBlob] = {}

    def blob(self, name: str) -> _GcsBlob:
        if name not in self.blobs:
            self.blobs[name] = _GcsBlob(name)
        return self.blobs[name]

    def list_blobs(self, prefix: str = ""):
        return [
            blob
            for name, blob in self.blobs.items()
            if name.startswith(prefix) and blob._data is not None
        ]


class _Gcs:
    def __init__(self) -> None:
        self._bucket = _GcsBucket()

    def bucket(self, _name: str) -> _GcsBucket:
        return self._bucket


def test_stitch_loader_splits_gcs_blip_from_confirmed_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("frontend.main.MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", "stitch-bucket")
    gcs = _Gcs()
    monkeypatch.setattr("frontend.main._get_gcs_client", lambda: gcs)
    blob = gcs.bucket("stitch-bucket").blob("vault-images/source.jpg")
    blob.upload_from_string(b"jpeg-bytes")
    assert load_stitch_media_bytes("source.jpg") == b"jpeg-bytes"

    blob.fail_download = True
    with pytest.raises(StitchTransient) as exc:
        load_stitch_media_bytes("source.jpg")
    assert exc.value.reason == "gcs_read_failed"
    assert load_uploaded_image_bytes("source.jpg") is None

    blob.fail_download = False
    blob.fail_exists = True
    with pytest.raises(StitchTransient) as exc:
        load_stitch_media_bytes("source.jpg")
    assert exc.value.reason == "gcs_read_failed"

    blob.fail_exists = False
    blob._data = None
    assert load_stitch_media_bytes("source.jpg") is None

    local = tmp_path / "local.jpg"
    local.write_bytes(b"local-bytes")

    def broken_open(*_args, **_kwargs):
        raise OSError("disk blip")

    monkeypatch.setattr("builtins.open", broken_open)
    with pytest.raises(StitchTransient) as exc:
        load_stitch_media_bytes("local.jpg")
    assert exc.value.reason == "gcs_read_failed"


@pytest.mark.asyncio
async def test_gcs_source_blip_stays_pending_and_missing_source_fails(
    stitch_env, monkeypatch
):
    """Scale-to-zero reads sources from GCS. A blip retries; absence is terminal."""
    gcs = _Gcs()
    monkeypatch.setattr("frontend.main.GCS_BUCKET_NAME", "stitch-bucket")
    monkeypatch.setattr("frontend.main.CLOUD_TASKS_QUEUE", "vault-ingest")
    monkeypatch.setattr("frontend.main._get_gcs_client", lambda: gcs)
    monkeypatch.setattr("frontend.main.create_ingest_cloud_task", lambda _job_id: None)
    composed = {"n": 0}
    real = stitch_mod.compose_vertical_jpeg

    def counting(parts):
        composed["n"] += 1
        return real(parts)

    monkeypatch.setattr(stitch_mod, "compose_vertical_jpeg", counting)

    async def succeed(*_args, **_kwargs):
        return "Stored after the bucket recovered"

    monkeypatch.setattr("frontend.main.ingest_uploaded_image", succeed)
    jpeg = _solid(4, 4, (12, 12, 12))
    from frontend.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post("/capture/stitch", files=_parts(jpeg, jpeg)[0])
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        record = load_job(job_id)
        ids = list(record["source_image_ids"])
        output_name = record["image_name"]
        source_names = [item["image_name"] for item in record["source_images"]]
        for name in source_names:
            (stitch_env / name).unlink()

        output_blob = gcs.bucket("stitch-bucket").blob(f"vault-images/{output_name}")
        output_blob.fail_exists = True
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        output_blip = await client.post("/ingest", json={"job_id": job_id})
        assert output_blip.status_code == 503
        assert output_blob.exists_calls >= 1
        assert composed["n"] == 0
        assert load_job(job_id)["status"] == "pending"
        assert load_job(job_id)["error"] is None
        assert load_job(job_id)["source_image_ids"] == ids
        output_blob.fail_exists = False

        source_blob = gcs.bucket("stitch-bucket").blob(
            f"vault-images/{source_names[0]}"
        )
        source_blob.fail_download = True
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        source_blip = await client.post("/ingest", json={"job_id": job_id})
        assert source_blip.status_code == 503
        assert source_blob.download_calls >= 1
        assert composed["n"] == 0
        assert load_job(job_id)["status"] == "pending"
        assert load_job(job_id)["error"] is None
        assert load_job(job_id)["source_image_ids"] == ids
        source_blob.fail_download = False

        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        recovered = await client.post("/ingest", json={"job_id": job_id})
        assert recovered.status_code == 200
        assert composed["n"] == 1
        assert (await client.get(f"/jobs/{job_id}")).json()["status"] == "succeeded"
        assert load_job(job_id)["source_image_ids"] == ids

        missing = await client.post("/capture/stitch", files=_parts(jpeg, jpeg)[0])
        missing_id = missing.json()["job_id"]
        missing_record = load_job(missing_id)
        gone_name = missing_record["source_images"][0]["image_name"]
        (stitch_env / gone_name).unlink()
        (stitch_env / missing_record["source_images"][1]["image_name"]).unlink()
        gone_blob = gcs.bucket("stitch-bucket").blob(f"vault-images/{gone_name}")
        gone_blob._data = None
        monkeypatch.setattr("frontend.main._ingest_in_flight", set())
        failed = await client.post("/ingest", json={"job_id": missing_id})
        assert failed.status_code == 200
        assert failed.json()["completed"] == [missing_id]
        body = (await client.get(f"/jobs/{missing_id}")).json()
        assert body["status"] == "failed"
        assert body["error"] == "stitch_source_missing"
        assert composed["n"] == 1
