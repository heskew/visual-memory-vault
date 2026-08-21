from google.adk.artifacts import InMemoryArtifactService
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from app.app_utils import services


def test_session_service_default():
    svc = services.get_session_service()
    assert isinstance(svc, InMemorySessionService)


def test_artifact_service_default():
    svc = services.get_artifact_service()
    assert isinstance(svc, InMemoryArtifactService)


def test_memory_service():
    svc = services.get_memory_service()
    assert svc is not None
