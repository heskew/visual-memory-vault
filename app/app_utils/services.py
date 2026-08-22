# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Process-wide ADK session/artifact services shared by every serving surface.

Registered under ``shared://`` so the ADK web routes, the A2A path, and the
reasoning_engine adapter share one instance: a session created on any surface
is visible to the others.
"""

from __future__ import annotations

import functools
import logging
import os

os.environ.setdefault("FLAIR_ALLOW_REMOTE_URL", "1")
os.environ.setdefault("FLAIR_TIMEOUT_SECONDS", "30.0")

from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.adk.cli.service_registry import get_service_registry
from google.adk.cli.utils.service_factory import create_session_service_from_options

logger = logging.getLogger(__name__)

SESSION_SERVICE_URI = "shared://session"
ARTIFACT_SERVICE_URI = "shared://artifact"
MEMORY_SERVICE_URI = "shared://memory"

_AGENT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

try:
    import adk_flair

    adk_flair.register()
except Exception as e:
    logger.debug("adk_flair registration notice: %s", e)


@functools.cache
def get_session_service():
    """Process-wide session service shared across every serving surface."""
    if uri := os.environ.get("SESSION_SERVICE_URI"):
        return create_session_service_from_options(
            base_dir=_AGENT_DIR, session_service_uri=uri
        )
    if agent_engine_id := os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID"):
        from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService

        return VertexAiSessionService(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            # Runtime-injected agent-engine region, not GOOGLE_CLOUD_LOCATION
            # (which agent.py pins to "global").
            location=os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION")
            or os.environ.get("GOOGLE_CLOUD_LOCATION"),
            agent_engine_id=agent_engine_id,
        )
    from google.adk.sessions.in_memory_session_service import InMemorySessionService

    return InMemorySessionService()


@functools.cache
def get_artifact_service():
    """Process-wide artifact service: GCS when a bucket is set, else in-memory."""
    if bucket := os.environ.get("LOGS_BUCKET_NAME"):
        return GcsArtifactService(bucket_name=bucket)
    return InMemoryArtifactService()


@functools.cache
def get_memory_service():
    """Process-wide memory service: FlairMemoryService if available, else InMemoryMemoryService."""
    flair_url = (
        os.environ.get("FLAIR_URL")
        or os.environ.get("FLAIR_TARGET")
        or "http://127.0.0.1:19926"
    )
    agent_id = os.environ.get("FLAIR_AGENT_ID", "visual-memory-vault")
    keyfile = os.environ.get("FLAIR_KEYFILE")
    if not keyfile:
        candidate = os.path.expanduser(f"~/.flair/keys/{agent_id}.key")
        if os.path.exists(candidate):
            keyfile = candidate
        elif os.path.exists(os.path.expanduser("~/.flair/keys/local.key")):
            keyfile = os.path.expanduser("~/.flair/keys/local.key")
            agent_id = "local"
        elif b64_key := os.environ.get("FLAIR_PRIVATE_KEY_B64"):
            import base64
            import tempfile

            try:
                raw_bytes = base64.b64decode(b64_key.strip())
            except Exception:
                raw_bytes = b64_key.strip().encode("utf-8")

            tmp = tempfile.NamedTemporaryFile("wb", delete=False, suffix=".key")
            tmp.write(raw_bytes)
            tmp.close()
            keyfile = tmp.name

    allow_remote = os.environ.get("FLAIR_ALLOW_REMOTE_URL") == "1" or not (
        "localhost" in flair_url or "127.0.0.1" in flair_url
    )
    if allow_remote:
        os.environ["FLAIR_ALLOW_REMOTE_URL"] = "1"
    if "FLAIR_TIMEOUT_SECONDS" not in os.environ:
        os.environ["FLAIR_TIMEOUT_SECONDS"] = "15.0"

    if keyfile and os.path.exists(keyfile):
        try:
            import adk_flair

            timeout_sec = float(os.environ.get("FLAIR_TIMEOUT_SECONDS", "30.0"))
            return adk_flair.FlairMemoryService(
                url=flair_url,
                agent_id=agent_id,
                keyfile=keyfile,
                timeout=timeout_sec,
            )
        except Exception as exc:
            logger.warning("Failed to initialize FlairMemoryService: %s", exc)

    from google.adk.memory.in_memory_memory_service import InMemoryMemoryService

    return InMemoryMemoryService()


_registry = get_service_registry()
_registry.register_session_service("shared", lambda uri, **kw: get_session_service())
_registry.register_artifact_service("shared", lambda uri, **kw: get_artifact_service())
_registry.register_memory_service("shared", lambda uri, **kw: get_memory_service())
