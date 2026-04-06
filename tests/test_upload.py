"""Tests for upload chunking and batch logic."""

import gzip
import json
import unittest.mock
from unittest.mock import MagicMock, patch

import httpx
import respx

from common_parlance.db import ConversationStore
from common_parlance.upload import (
    MAX_RETRIES,
    _backoff_delay,
    _chunk_rows,
    _upload_one_chunk,
    upload_batch,
)

# --- Backoff ---


def test_backoff_delay_increases():
    delays = [_backoff_delay(i) for i in range(5)]
    # Each base should roughly double (with jitter)
    assert delays[0] < delays[2]
    assert delays[2] < delays[4]


def test_backoff_delay_capped():
    delay = _backoff_delay(100)
    # Cap is 300 + up to 50% jitter = max 450
    assert delay <= 450


# --- Chunking ---


def _make_row(content_size=100):
    """Create a mock DB row dict."""
    turns = [{"role": "user", "content": "x" * content_size}]
    return {
        "id": "test-id",
        "scrubbed_turns": json.dumps(turns),
        "turn_count": 1,
        "language": "en",
        "quality_signals": json.dumps({}),
        "ner_scrubbed": 1,
    }


def test_chunk_rows_single_chunk():
    rows = [_make_row(100) for _ in range(5)]
    chunks = _chunk_rows(rows)
    assert len(chunks) == 1
    assert len(chunks[0]) == 5


def test_chunk_rows_splits_large_batch():
    # Create rows that are ~500 bytes each, with a 1KB limit
    rows = [_make_row(400) for _ in range(5)]
    chunks = _chunk_rows(rows, max_bytes=1000)
    assert len(chunks) > 1
    # All rows should be accounted for
    total = sum(len(c) for c in chunks)
    assert total == 5


def test_chunk_rows_empty():
    chunks = _chunk_rows([])
    assert chunks == []


def test_chunk_rows_single_oversized_row():
    """A single row larger than max_bytes still gets its own chunk."""
    rows = [_make_row(5000)]
    chunks = _chunk_rows(rows, max_bytes=1000)
    assert len(chunks) == 1
    assert len(chunks[0]) == 1


# --- upload_batch ---


def test_upload_batch_no_api_key():
    store = MagicMock()
    result = upload_batch(store, api_key="")
    assert result == 0
    store.get_ready_for_upload.assert_not_called()


def test_upload_batch_nothing_to_upload():
    store = MagicMock()
    store.get_ready_for_upload.return_value = []
    result = upload_batch(store, api_key="test-key")
    assert result == 0


@patch("common_parlance.upload._upload_one_chunk")
def test_upload_batch_success(mock_upload):
    mock_upload.return_value = "ok"

    store = MagicMock()
    rows = [_make_row() for _ in range(3)]
    store.get_ready_for_upload.return_value = rows

    result = upload_batch(store, api_key="test-key")
    assert result == 3
    store.mark_uploaded.assert_called_once()
    store.purge_processed_raw.assert_called_once()


@patch("common_parlance.upload._upload_one_chunk")
def test_upload_batch_auth_failure_stops(mock_upload):
    mock_upload.return_value = "auth"

    store = MagicMock()
    store.get_ready_for_upload.return_value = [_make_row()]

    result = upload_batch(store, api_key="test-key")
    assert result == 0
    store.mark_uploaded.assert_not_called()


@patch("common_parlance.upload._upload_one_chunk")
def test_upload_batch_rejection_isolates(mock_upload):
    """On chunk rejection, individual rows are retried."""
    # First call (full chunk) rejected, then individual retries succeed
    mock_upload.side_effect = ["rejected", "ok", "ok"]

    store = MagicMock()
    rows = [_make_row() for _ in range(2)]
    # Give each row a unique id
    rows[0]["id"] = "row-1"
    rows[1]["id"] = "row-2"
    store.get_ready_for_upload.return_value = rows

    result = upload_batch(store, api_key="test-key")
    assert result == 2


@patch("common_parlance.upload._upload_one_chunk")
def test_upload_batch_transient_error_stops(mock_upload):
    mock_upload.return_value = "error"

    store = MagicMock()
    store.get_ready_for_upload.return_value = [_make_row()]

    result = upload_batch(store, api_key="test-key")
    assert result == 0


@patch("common_parlance.upload._upload_one_chunk")
def test_upload_batch_releases_unclaimed_on_auth_error(mock_upload):
    """Rows not yet processed when auth error breaks loop get released."""
    mock_upload.return_value = "auth"

    store = MagicMock()
    rows = [_make_row() for _ in range(3)]
    for i, r in enumerate(rows):
        r["id"] = f"row-{i}"
    store.get_ready_for_upload.return_value = rows

    upload_batch(store, api_key="test-key")
    store.release_upload_claim.assert_called_once()


# --- _upload_one_chunk ---


def _make_chunk(n=1):
    """Create a chunk (list of (row, line) tuples) for upload tests."""
    chunk = []
    for i in range(n):
        row = _make_row()
        row["id"] = f"chunk-row-{i}"
        record = {
            "conversation_id": "test-conv-id",
            "turns": json.loads(row["scrubbed_turns"]),
            "turn_count": row["turn_count"],
            "language": row["language"],
            "quality_signals": json.loads(row["quality_signals"]),
            "ner_scrubbed": bool(row["ner_scrubbed"]),
        }
        line = json.dumps(record)
        chunk.append((row, line))
    return chunk


_DUMMY_REQUEST = httpx.Request("POST", "https://proxy.example.com/upload")


@patch("common_parlance.upload.httpx.post")
def test_upload_one_chunk_success(mock_post):
    mock_post.return_value = httpx.Response(
        200, json={"ok": True}, request=_DUMMY_REQUEST
    )

    chunk = _make_chunk()
    result = _upload_one_chunk(chunk, "https://proxy.example.com", "key")

    assert result == "ok"
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert call_kwargs.kwargs["headers"]["X-API-Key"] == "key"
    assert call_kwargs.kwargs["headers"]["Content-Encoding"] == "gzip"
    # Verify content is gzip-compressed
    raw = gzip.decompress(call_kwargs.kwargs["content"])
    assert b"test-conv-id" in raw


@patch("common_parlance.upload.httpx.post")
def test_upload_one_chunk_auth_failure(mock_post):
    mock_post.return_value = httpx.Response(401, request=_DUMMY_REQUEST)

    result = _upload_one_chunk(_make_chunk(), "https://proxy.example.com", "bad-key")
    assert result == "auth"
    # Should not retry on 401
    assert mock_post.call_count == 1


@patch("common_parlance.upload.httpx.post")
def test_upload_one_chunk_rejection(mock_post):
    mock_post.return_value = httpx.Response(
        422, json={"error": "PII detected"}, request=_DUMMY_REQUEST
    )

    result = _upload_one_chunk(_make_chunk(), "https://proxy.example.com", "key")
    assert result == "rejected"
    assert mock_post.call_count == 1


@patch("common_parlance.upload.time.sleep")
@patch("common_parlance.upload.httpx.post")
def test_upload_one_chunk_rate_limit_retries(mock_post, mock_sleep):
    """429 triggers retry with backoff."""
    mock_post.side_effect = [
        httpx.Response(429, request=_DUMMY_REQUEST),
        httpx.Response(200, json={"ok": True}, request=_DUMMY_REQUEST),
    ]

    result = _upload_one_chunk(_make_chunk(), "https://proxy.example.com", "key")
    assert result == "ok"
    assert mock_post.call_count == 2
    mock_sleep.assert_called_once()


@patch("common_parlance.upload.time.sleep")
@patch("common_parlance.upload.httpx.post")
def test_upload_one_chunk_connect_error_retries(mock_post, mock_sleep):
    """Connection errors retry up to MAX_RETRIES."""
    mock_post.side_effect = httpx.ConnectError("refused")

    result = _upload_one_chunk(_make_chunk(), "https://proxy.example.com", "key")
    assert result == "error"
    assert mock_post.call_count == 5  # MAX_RETRIES


@patch("common_parlance.upload.time.sleep")
@patch("common_parlance.upload.httpx.post")
def test_upload_one_chunk_timeout_retries(mock_post, mock_sleep):
    """Timeout errors retry then succeed."""
    mock_post.side_effect = [
        httpx.TimeoutException("timeout"),
        httpx.Response(200, json={"ok": True}, request=_DUMMY_REQUEST),
    ]

    result = _upload_one_chunk(_make_chunk(), "https://proxy.example.com", "key")
    assert result == "ok"
    assert mock_post.call_count == 2


# ── respx-based HTTP tests (real httpx transport mocking) ────────────

RESPX_PROXY = "https://respx-proxy.example.com"
RESPX_KEY = "respx-test-key"


@respx.mock
def test_respx_upload_one_chunk_ok():
    """200 response returns 'ok' (respx)."""
    respx.post(f"{RESPX_PROXY}/upload").respond(200, json={"status": "ok"})
    chunk = _make_chunk()
    result = _upload_one_chunk(chunk, RESPX_PROXY, RESPX_KEY)
    assert result == "ok"


@respx.mock
def test_respx_upload_one_chunk_auth():
    """401 response returns 'auth' (respx)."""
    respx.post(f"{RESPX_PROXY}/upload").respond(401)
    result = _upload_one_chunk(_make_chunk(), RESPX_PROXY, RESPX_KEY)
    assert result == "auth"


@respx.mock
def test_respx_upload_one_chunk_rejected():
    """422 response returns 'rejected' (respx)."""
    respx.post(f"{RESPX_PROXY}/upload").respond(422, json={"error": "content policy"})
    result = _upload_one_chunk(_make_chunk(), RESPX_PROXY, RESPX_KEY)
    assert result == "rejected"


@unittest.mock.patch("common_parlance.upload.time.sleep")
@respx.mock
def test_respx_retry_on_429(mock_sleep):
    """429 then 200 retries and returns 'ok' (respx)."""
    route = respx.post(f"{RESPX_PROXY}/upload")
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json={"status": "ok"}),
    ]
    result = _upload_one_chunk(_make_chunk(), RESPX_PROXY, RESPX_KEY)
    assert result == "ok"
    assert mock_sleep.call_count == 1


@unittest.mock.patch("common_parlance.upload.time.sleep")
@respx.mock
def test_respx_server_error_exhausts_retries(mock_sleep):
    """502 errors exhaust retries and return 'error' (respx)."""
    respx.post(f"{RESPX_PROXY}/upload").respond(502)
    result = _upload_one_chunk(_make_chunk(), RESPX_PROXY, RESPX_KEY)
    assert result == "error"
    assert mock_sleep.call_count == MAX_RETRIES - 1


@unittest.mock.patch("common_parlance.upload.time.sleep")
@respx.mock
def test_respx_connect_error_retries(mock_sleep):
    """Connection errors retry then return 'error' (respx)."""
    respx.post(f"{RESPX_PROXY}/upload").mock(side_effect=httpx.ConnectError("refused"))
    result = _upload_one_chunk(_make_chunk(), RESPX_PROXY, RESPX_KEY)
    assert result == "error"
    assert mock_sleep.call_count == MAX_RETRIES - 1


@respx.mock
def test_respx_sends_correct_headers():
    """Verify API key and content headers via respx."""
    route = respx.post(f"{RESPX_PROXY}/upload").respond(200, json={"ok": True})
    _upload_one_chunk(_make_chunk(), RESPX_PROXY, RESPX_KEY)
    request = route.calls[0].request
    assert request.headers["X-API-Key"] == RESPX_KEY
    assert request.headers["Content-Type"] == "application/x-ndjson"
    assert request.headers["Content-Encoding"] == "gzip"
    # Verify body is valid gzip JSONL
    raw = gzip.decompress(request.content)
    assert b"test-conv-id" in raw


@respx.mock
def test_respx_sends_gzip_body():
    """Verify the body is gzip-compressed JSONL (respx)."""
    route = respx.post(f"{RESPX_PROXY}/upload").respond(200, json={"ok": True})
    _upload_one_chunk(_make_chunk(3), RESPX_PROXY, RESPX_KEY)
    raw = gzip.decompress(route.calls[0].request.content)
    lines = raw.decode("utf-8").strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        obj = json.loads(line)
        assert "conversation_id" in obj
        assert "turns" in obj


# ── respx-based upload_batch with real SQLite ────────────────────────


def _seed_real_store(store, n=3):
    """Insert n exchanges into a real ConversationStore, process and approve."""
    staged_ids = []
    for i in range(n):
        eid = store.log_exchange(
            f"sess-{i}",
            json.dumps(
                {"messages": [{"role": "user", "content": f"Question {i} " + "x" * 60}]}
            ),
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": f"Answer {i} " + "y" * 60,
                            }
                        }
                    ]
                }
            ),
        )
        sid = store.mark_processed(
            eid,
            json.dumps(
                [
                    {"role": "user", "content": f"Question {i} " + "x" * 60},
                    {"role": "assistant", "content": f"Answer {i} " + "y" * 60},
                ]
            ),
            ner_scrubbed=True,
            turn_count=2,
            language="en",
            quality_signals=json.dumps({"avg_response_len": 70}),
        )
        staged_ids.append(sid)
    store.approve_batch(staged_ids)
    return staged_ids


@unittest.mock.patch("common_parlance.upload.time.sleep")
@respx.mock
def test_respx_upload_batch_success(mock_sleep, tmp_path):
    """Upload approved conversations via respx and verify DB state."""
    store = ConversationStore(str(tmp_path / "test.db"))
    _seed_real_store(store, 3)

    respx.post(f"{RESPX_PROXY}/upload").respond(200, json={"status": "ok"})
    count = upload_batch(store, proxy_url=RESPX_PROXY, api_key=RESPX_KEY, limit=100)

    assert count == 3
    stats = store.stats()
    assert stats["uploaded"] == 3
    assert stats["approved"] == 0
    store.close()


@unittest.mock.patch("common_parlance.upload.time.sleep")
@respx.mock
def test_respx_upload_batch_empty(mock_sleep, tmp_path):
    """Empty queue returns 0 (respx)."""
    store = ConversationStore(str(tmp_path / "test.db"))
    count = upload_batch(store, proxy_url=RESPX_PROXY, api_key=RESPX_KEY)
    assert count == 0
    store.close()


@unittest.mock.patch("common_parlance.upload.time.sleep")
@respx.mock
def test_respx_upload_batch_releases_on_failure(mock_sleep, tmp_path):
    """Transient failure releases claims (respx)."""
    store = ConversationStore(str(tmp_path / "test.db"))
    _seed_real_store(store, 2)

    respx.post(f"{RESPX_PROXY}/upload").respond(502)
    count = upload_batch(store, proxy_url=RESPX_PROXY, api_key=RESPX_KEY)

    assert count == 0
    stats = store.stats()
    assert stats["approved"] == 2
    assert stats["uploaded"] == 0
    store.close()


@unittest.mock.patch("common_parlance.upload.time.sleep")
@respx.mock
def test_respx_upload_batch_chunking(mock_sleep, tmp_path):
    """Large batch gets chunked into multiple HTTP requests (respx)."""
    store = ConversationStore(str(tmp_path / "test.db"))
    _seed_real_store(store, 10)

    route = respx.post(f"{RESPX_PROXY}/upload").respond(200, json={"ok": True})

    orig_chunk = _chunk_rows
    with unittest.mock.patch(
        "common_parlance.upload._chunk_rows",
        side_effect=lambda rows, **kw: orig_chunk(rows, max_bytes=200),
    ):
        count = upload_batch(store, proxy_url=RESPX_PROXY, api_key=RESPX_KEY, limit=100)

    assert count == 10
    assert route.call_count > 1
    stats = store.stats()
    assert stats["uploaded"] == 10
    store.close()
