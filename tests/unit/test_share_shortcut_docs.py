"""The phone Share recipe stays aligned with the 202 capture routes."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text()
RECIPE = (ROOT / "docs/shortcuts/share-to-vault.md").read_text()
ONBOARDING = (ROOT / "docs/ONBOARDING.md").read_text()
AGENT_ACCESS = (ROOT / "docs/agent-access.md").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()
# Personal proxy host. Docs must not advertise it.
FORBIDDEN_HOST = "https://visual-memory-vault-proxy-151358874679.us-east1.run.app"


def _mobile_section() -> str:
    start = README.index("## 📱 Mobile Ingestion")
    rest = README[start:]
    nxt = rest.find("\n## ", 1)
    return rest if nxt < 0 else rest[:nxt]


def test_share_docs_do_not_advertise_a_personal_proxy_host():
    for text in (README, RECIPE, ONBOARDING, AGENT_ACCESS, ENV_EXAMPLE):
        assert FORBIDDEN_HOST not in text
        assert "151358874679" not in text
        assert "us-east1.run.app" not in text


def test_readme_mobile_ingestion_covers_share_routes():
    section = _mobile_section()
    assert "https://YOUR_VAULT_PROXY" in section
    assert "http://localhost:8080" in section
    assert "docs/shortcuts/share-to-vault.md" in section
    assert "`POST /upload`" in section
    assert "`POST /capture/stitch`" in section
    assert "`POST /capture/url`" in section
    assert "X-Api-Key" in section
    assert "job_id" in section
    assert "202" in section
    assert "not published yet" in section
    assert "Copy iCloud Link" in section
    assert "no default" in section
    assert "/jobs/" in section
    assert "POST /ingest" in section


def test_shortcut_recipe_routes_without_embedding_a_secret():
    assert "https://YOUR_VAULT_PROXY/upload" in RECIPE
    assert "https://YOUR_VAULT_PROXY/capture/stitch" in RECIPE
    assert "https://YOUR_VAULT_PROXY/capture/url" in RECIPE
    assert "POST /upload" in RECIPE
    assert "POST /capture/stitch" in RECIPE
    assert "POST /capture/url" in RECIPE
    assert "X-Api-Key" in RECIPE
    assert '"status": "accepted"' in RECIPE
    assert "job_id" in RECIPE
    assert "202" in RECIPE
    assert "Import Question" in RECIPE
    assert "no default" in RECIPE
    assert "Copy iCloud Link" in RECIPE
    assert "not published yet" in RECIPE
    assert "Share 8 images or fewer." in RECIPE
    assert "Not queued" in RECIPE
    assert "Queued" in RECIPE
    assert "Run JavaScript on Webpage" in RECIPE
    assert "Do not type the API key into this file" in RECIPE
    assert "does not name a shared host" in RECIPE
    assert "icloud.com/shortcuts/" not in RECIPE.lower()
    assert "PROXY_API_KEY=" not in RECIPE


def test_onboarding_points_at_the_same_share_recipe():
    assert "shortcuts/share-to-vault.md" in ONBOARDING
    assert "https://YOUR_VAULT_PROXY/upload" in ONBOARDING
    assert "/capture/stitch" in ONBOARDING
    assert "/capture/url" in ONBOARDING
