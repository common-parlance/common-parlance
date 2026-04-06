"""Tests for the proxy server."""

import json

import httpx
import pytest
from starlette.testclient import TestClient

from common_parlance.proxy import _forward_headers, create_app, is_chat_endpoint

# --- Pure function tests ---


def test_is_chat_endpoint_openai():
    assert is_chat_endpoint("/v1/chat/completions") is True


def test_is_chat_endpoint_ollama_chat():
    assert is_chat_endpoint("/api/chat") is True


def test_is_chat_endpoint_ollama_generate():
    assert is_chat_endpoint("/api/generate") is True


def test_is_chat_endpoint_non_chat():
    assert is_chat_endpoint("/api/tags") is False
    assert is_chat_endpoint("/v1/models") is False
    assert is_chat_endpoint("/health") is False


def test_is_chat_endpoint_with_prefix():
    """Paths containing chat paths still match."""
    assert is_chat_endpoint("/prefix/v1/chat/completions") is True


def test_forward_headers_strips_hop_by_hop():
    raw = [
        ("content-type", "application/json"),
        ("host", "localhost"),
        ("transfer-encoding", "chunked"),
        ("x-custom", "keep-me"),
    ]
    result = _forward_headers(raw)
    assert "content-type" in result
    assert "x-custom" in result
    assert "host" not in result
    assert "transfer-encoding" not in result


# --- Integration tests with TestClient ---


@pytest.fixture
def mock_upstream(httpx_mock):
    """Fixture that provides a mock upstream URL."""
    return "http://mock-upstream:11434"


@pytest.fixture
def httpx_mock(monkeypatch):
    """Simple httpx mock that intercepts AsyncClient requests."""
    responses = {}

    class MockAsyncClient:
        def __init__(self, **kwargs):
            self.base_url = kwargs.get("base_url", "")

        async def request(self, method, url, **kwargs):
            key = f"{method}:{url}"
            if key in responses:
                data = responses[key]
                return httpx.Response(
                    data.get("status", 200),
                    json=data.get("json"),
                    text=data.get("text", ""),
                    headers=data.get("headers", {}),
                )
            return httpx.Response(200, text="default response")

        def build_request(self, **kwargs):
            return httpx.Request(kwargs["method"], kwargs["url"])

        async def send(self, req, stream=False):
            return await self.request(req.method, str(req.url))

        async def aclose(self):
            pass

    mock = MockAsyncClient
    mock.responses = responses
    monkeypatch.setattr("common_parlance.proxy.httpx.AsyncClient", mock)
    return mock


def test_proxy_forwards_non_chat_request(httpx_mock):
    """Non-chat requests are forwarded without logging."""
    app = create_app("http://mock-upstream:11434", db_path=None)
    client = TestClient(app)

    resp = client.get("/api/tags")
    assert resp.status_code == 200


def test_proxy_forwards_chat_request_no_db(httpx_mock):
    """Chat requests work without a DB path (logging disabled)."""
    app = create_app("http://mock-upstream:11434", db_path=None)
    client = TestClient(app)

    body = {"messages": [{"role": "user", "content": "Hello"}]}
    resp = client.post(
        "/v1/chat/completions",
        json=body,
    )
    assert resp.status_code == 200


def test_proxy_logs_chat_to_db(httpx_mock, tmp_path):
    """Chat requests are logged to the database when db_path is set."""
    from common_parlance.db import ConversationStore

    db_path = str(tmp_path / "test.db")
    # Initialize DB schema
    with ConversationStore(db_path):
        pass

    # Set up mock response as JSON
    httpx_mock.responses["POST:/v1/chat/completions"] = {
        "status": 200,
        "json": {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hi there!",
                    }
                }
            ]
        },
    }

    app = create_app("http://mock-upstream:11434", db_path=db_path)
    client = TestClient(app)

    body = {"messages": [{"role": "user", "content": "Hello"}]}
    resp = client.post("/v1/chat/completions", json=body)
    assert resp.status_code == 200

    # Verify exchange was logged
    with ConversationStore(db_path) as store:
        exchanges = store.get_unprocessed()
        assert len(exchanges) == 1
        req_json = json.loads(exchanges[0]["request_json"])
        assert req_json["messages"][0]["content"] == "Hello"


def test_proxy_non_chat_not_logged(httpx_mock, tmp_path):
    """Non-chat requests are NOT logged to the database."""
    from common_parlance.db import ConversationStore

    db_path = str(tmp_path / "test.db")
    with ConversationStore(db_path):
        pass

    app = create_app("http://mock-upstream:11434", db_path=db_path)
    client = TestClient(app)

    resp = client.get("/api/tags")
    assert resp.status_code == 200

    with ConversationStore(db_path) as store:
        assert len(store.get_unprocessed()) == 0


def test_proxy_upstream_error_returns_502(monkeypatch):
    """Connection error to upstream returns 502."""

    class FailingClient:
        def __init__(self, **kwargs):
            pass

        async def request(self, method, url, **kwargs):
            raise httpx.ConnectError("Connection refused")

        def build_request(self, **kwargs):
            return httpx.Request(kwargs["method"], kwargs["url"])

        async def send(self, req, stream=False):
            raise httpx.ConnectError("Connection refused")

        async def aclose(self):
            pass

    monkeypatch.setattr("common_parlance.proxy.httpx.AsyncClient", FailingClient)

    app = create_app("http://bad-host:11434", db_path=None)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "test"}]},
    )
    assert resp.status_code == 502


def test_proxy_logging_failure_doesnt_break_response(httpx_mock, tmp_path, monkeypatch):
    """If DB logging fails, the proxy still returns the response."""
    monkeypatch.setattr(
        "common_parlance.proxy._log_exchange_sync",
        lambda *args: (_ for _ in ()).throw(RuntimeError("DB broken")),
    )

    app = create_app(
        "http://mock-upstream:11434",
        db_path=str(tmp_path / "test.db"),
    )
    client = TestClient(app)

    body = {"messages": [{"role": "user", "content": "Hello"}]}
    resp = client.post("/v1/chat/completions", json=body)
    # Response should still succeed even though logging failed
    assert resp.status_code == 200
