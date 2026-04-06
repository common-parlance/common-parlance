"""Full pipeline integration test: import -> process -> approve -> upload."""

import json
import unittest.mock
from pathlib import Path

import respx

from common_parlance.db import ConversationStore
from common_parlance.filter import create_content_filter
from common_parlance.importers import import_conversations
from common_parlance.process import process_batch
from common_parlance.scrub import RegexScrubber
from common_parlance.upload import upload_batch

PROXY_URL = "https://pipeline-test.example.com"
API_KEY = "pipeline-test-key"


def _write_fixture(tmp_path: Path) -> Path:
    """Write a JSONL fixture with 3 sample conversations."""
    fixture = tmp_path / "conversations.jsonl"
    conversations = [
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "What is the capital of France? Can you explain in detail?"
                    ),
                },
                {
                    "role": "assistant",
                    "content": (
                        "The capital of France is Paris."
                        " It has been the capital"
                        " since the 10th century."
                    ),
                },
            ]
        },
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Write me a simple hello world program in Python please."
                    ),
                },
                {
                    "role": "assistant",
                    "content": (
                        "Here is a simple hello world:\n"
                        "```python\nprint('Hello, world!')\n```"
                    ),
                },
            ]
        },
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Can you explain how photosynthesis works in plants?",
                },
                {
                    "role": "assistant",
                    "content": (
                        "Photosynthesis converts sunlight"
                        " into chemical energy. Plants"
                        " absorb CO2 and water, producing"
                        " glucose and oxygen."
                    ),
                },
            ]
        },
    ]
    lines = [json.dumps(c) for c in conversations]
    fixture.write_text("\n".join(lines), encoding="utf-8")
    return fixture


@unittest.mock.patch("common_parlance.upload.time.sleep")
@respx.mock
def test_full_pipeline(mock_sleep, tmp_path):
    """End-to-end: import -> process -> approve -> upload."""
    # 1. Create fixture and store
    fixture_path = _write_fixture(tmp_path)
    db_path = str(tmp_path / "pipeline.db")
    store = ConversationStore(db_path)

    # 2. Import conversations from JSONL fixture
    result = import_conversations(store=store, path=fixture_path)
    assert result.imported == 3
    assert result.errors == []

    stats = store.stats()
    assert stats["raw"] == 3

    # 3. Process through scrubbing pipeline (regex-only, no NER)
    scrubber = RegexScrubber()
    content_filter = create_content_filter()
    processed = process_batch(store, scrubber, content_filter=content_filter)
    assert processed == 3

    stats = store.stats()
    assert stats["raw"] == 0
    assert stats["pending_review"] == 3

    # 4. Approve all pending conversations
    pending = store.get_pending_review(limit=50)
    assert len(pending) == 3
    staged_ids = [row["id"] for row in pending]
    store.approve_batch(staged_ids)

    stats = store.stats()
    assert stats["approved"] == 3

    # 5. Upload via mocked Worker endpoint
    route = respx.post(f"{PROXY_URL}/upload").respond(200, json={"status": "ok"})
    count = upload_batch(store, proxy_url=PROXY_URL, api_key=API_KEY, limit=100)

    # 6. Assertions
    assert count == 3
    assert route.called
    assert route.call_count >= 1

    stats = store.stats()
    assert stats["uploaded"] == 3
    assert stats["approved"] == 0
    assert stats["pending_review"] == 0

    store.close()


@unittest.mock.patch("common_parlance.upload.time.sleep")
@respx.mock
def test_pipeline_upload_failure_preserves_data(mock_sleep, tmp_path):
    """Pipeline handles upload failure gracefully: data stays approved."""
    fixture_path = _write_fixture(tmp_path)
    db_path = str(tmp_path / "pipeline_fail.db")
    store = ConversationStore(db_path)

    # Import, process, approve
    import_conversations(store=store, path=fixture_path)
    scrubber = RegexScrubber()
    process_batch(store, scrubber)
    pending = store.get_pending_review(limit=50)
    store.approve_batch([row["id"] for row in pending])

    # Upload fails with 502
    respx.post(f"{PROXY_URL}/upload").respond(502)
    count = upload_batch(store, proxy_url=PROXY_URL, api_key=API_KEY)

    assert count == 0
    stats = store.stats()
    assert stats["uploaded"] == 0
    # Data should still be approved and available for retry
    assert stats["approved"] == 3

    store.close()


@unittest.mock.patch("common_parlance.upload.time.sleep")
@respx.mock
def test_pipeline_empty_import(mock_sleep, tmp_path):
    """Pipeline handles empty import gracefully."""
    fixture = tmp_path / "empty.jsonl"
    fixture.write_text("", encoding="utf-8")
    db_path = str(tmp_path / "pipeline_empty.db")
    store = ConversationStore(db_path)

    result = import_conversations(store=store, path=fixture)
    assert result.imported == 0

    scrubber = RegexScrubber()
    processed = process_batch(store, scrubber)
    assert processed == 0

    count = upload_batch(store, proxy_url=PROXY_URL, api_key=API_KEY)
    assert count == 0

    store.close()


@unittest.mock.patch("common_parlance.upload.time.sleep")
@respx.mock
def test_pipeline_dedup_on_reimport(mock_sleep, tmp_path):
    """Re-importing the same file does not create duplicates."""
    fixture_path = _write_fixture(tmp_path)
    db_path = str(tmp_path / "pipeline_dedup.db")
    store = ConversationStore(db_path)

    result1 = import_conversations(store=store, path=fixture_path)
    assert result1.imported == 3

    result2 = import_conversations(store=store, path=fixture_path)
    assert result2.imported == 0
    assert result2.skipped_duplicate == 3

    stats = store.stats()
    assert stats["raw"] == 3  # Only 3, not 6

    store.close()
