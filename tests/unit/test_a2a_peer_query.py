import importlib.util
import pathlib

_MOD_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "examples" / "a2a_peer_query.py"
)
_spec = importlib.util.spec_from_file_location("a2a_peer_query", _MOD_PATH)
peer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(peer)


def test_a2a_base_url_builds_expected_path():
    assert peer.a2a_base_url("http://127.0.0.1:8000", "app") == (
        "http://127.0.0.1:8000/a2a/app"
    )
    # trailing slash on the base is normalized
    assert peer.a2a_base_url("http://host:8000/", "app") == "http://host:8000/a2a/app"


def test_card_url_for_appends_well_known():
    base = "http://127.0.0.1:8000/a2a/app"
    assert peer.card_url_for(base) == base + "/.well-known/agent-card.json"


def test_card_url_round_trips_to_base():
    base = "https://example.run.app/a2a/app"
    card = peer.card_url_for(base)
    assert peer.a2a_base_from_card_url(card) == base


def test_a2a_base_from_card_url_without_suffix():
    assert (
        peer.a2a_base_from_card_url("https://host/a2a/app/") == "https://host/a2a/app"
    )


def test_extract_text_parts_handles_dicts_objects_and_empties():
    class _Part:
        def __init__(self, text=None):
            self.text = text

    parts = [
        {"text": "hello"},
        {"text": ""},
        {"data": "not text"},
        _Part("world"),
        _Part(None),
    ]
    assert peer.extract_text_parts(parts) == ["hello", "world"]
    assert peer.extract_text_parts(None) == []
