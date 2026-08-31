"""Guard Cloud Run install pins against pyproject drift.

frontend/Dockerfile copies frontend/requirements.txt and pip-installs it.
A rebuild from main ImportErrors on Cloud Tasks enqueue if that file drifts
from the google-cloud-tasks pin already in pyproject.toml.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUD_TASKS_NAME = "google-cloud-tasks"
EXPECTED_CLOUD_TASKS_PIN = "google-cloud-tasks>=2.16.0,<3.0.0"


def _requirement_name(line: str) -> str:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return ""
    for sep in ("[", ";", ">", "<", "=", "!", "~", " "):
        raw = raw.split(sep, 1)[0]
    return raw


def _pyproject_deps() -> list[str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return list(data["project"]["dependencies"])


def _requirements_lines() -> list[str]:
    return [
        line.strip()
        for line in (REPO_ROOT / "frontend" / "requirements.txt")
        .read_text()
        .splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_frontend_requirements_pins_google_cloud_tasks_like_pyproject():
    pins = [
        dep for dep in _pyproject_deps() if _requirement_name(dep) == CLOUD_TASKS_NAME
    ]
    assert pins == [EXPECTED_CLOUD_TASKS_PIN]

    matching = [
        line
        for line in _requirements_lines()
        if _requirement_name(line) == CLOUD_TASKS_NAME
    ]
    assert matching == pins, (
        "frontend/requirements.txt must include the same google-cloud-tasks pin as "
        f"pyproject.toml ({pins[0]}); Cloud Run installs only requirements.txt"
    )
