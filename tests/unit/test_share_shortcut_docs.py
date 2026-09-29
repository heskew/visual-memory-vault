"""The phone Share recipe stays aligned with the 202 capture routes."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text()
RECIPE = (ROOT / "docs/shortcuts/share-to-vault.md").read_text()
ONBOARDING = (ROOT / "docs/ONBOARDING.md").read_text()
HOST = "https://visual-memory-vault-proxy-151358874679.us-east1.run.app"


def _mobile_section() -> str:
    start = README.index("## 📱 Mobile Ingestion")
    rest = README[start:]
    nxt = rest.find("\n## ", 1)
    return rest if nxt < 0 else rest[:nxt]


def test_readme_mobile_ingestion_covers_share_routes():
    section = _mobile_section()
    assert HOST in section
    assert "docs/shortcuts/share-to-vault.md" in section
    assert "`POST /upload`" in section
    assert "`POST /capture/stitch`" in section
    assert "`POST /capture/url`" in section
    assert "X-Api-Key" in section
    assert "job_id" in section
    assert "202" in section
    assert "not published yet" in section
    assert "Copy iCloud Link" in section
    assert "GET /jobs" in section or "/jobs/" in section
    assert "POST /ingest" in section


def test_shortcut_recipe_routes_without_embedding_a_secret():
    assert HOST in RECIPE
    assert "POST /upload" in RECIPE
    assert "POST /capture/stitch" in RECIPE
    assert "POST /capture/url" in RECIPE
    assert "X-Api-Key" in RECIPE
    assert '"status": "accepted"' in RECIPE
    assert "job_id" in RECIPE
    assert "202" in RECIPE
    assert "Import Question" in RECIPE
    assert "Copy iCloud Link" in RECIPE
    assert "not published yet" in RECIPE
    assert "Share 8 images or fewer." in RECIPE
    assert "Not queued" in RECIPE
    assert "Queued" in RECIPE
    assert "Run JavaScript on Webpage" in RECIPE
    assert "Do not type the API key into this file" in RECIPE
    assert "icloud.com/shortcuts/" not in RECIPE.lower()
    assert "PROXY_API_KEY=" not in RECIPE
    for path in ("/upload", "/capture/stitch", "/capture/url"):
        assert f"{HOST}{path}" in RECIPE


def test_onboarding_points_at_the_same_share_recipe():
    assert "shortcuts/share-to-vault.md" in ONBOARDING
    assert "/capture/stitch" in ONBOARDING
    assert "/capture/url" in ONBOARDING
