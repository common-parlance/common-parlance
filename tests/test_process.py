"""Tests for quality signal computation and batch processing."""

import json

import pytest

from common_parlance.db import ConversationStore
from common_parlance.process import compute_quality_signals, process_batch
from common_parlance.scrub import RegexScrubber


def test_basic_signals():
    turns = [
        {"role": "user", "content": "What is Python?"},
        {
            "role": "assistant",
            "content": "Python is a programming language used for many things.",
        },
    ]
    signals = compute_quality_signals(turns)
    assert signals["user_msg_count"] == 1
    assert signals["assistant_msg_count"] == 1
    assert signals["avg_response_len"] == len(turns[1]["content"])
    assert signals["has_code"] is False
    assert signals["total_length"] == sum(len(t["content"]) for t in turns)
    assert 0 < signals["vocab_diversity"] <= 1.0


def test_code_block_detection():
    turns = [
        {"role": "user", "content": "Show me hello world"},
        {
            "role": "assistant",
            "content": "Here you go:\n```python\nprint('hello')\n```",
        },
    ]
    signals = compute_quality_signals(turns)
    assert signals["has_code"] is True


def test_no_code_block():
    turns = [
        {"role": "user", "content": "Tell me a joke"},
        {"role": "assistant", "content": "Why did the chicken cross the road?"},
    ]
    signals = compute_quality_signals(turns)
    assert signals["has_code"] is False


def test_multi_turn():
    turns = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello! How can I help?"},
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "4"},
    ]
    signals = compute_quality_signals(turns)
    assert signals["user_msg_count"] == 2
    assert signals["assistant_msg_count"] == 2
    assert signals["avg_response_len"] == round(
        (len("Hello! How can I help?") + len("4")) / 2
    )


def test_empty_turns():
    signals = compute_quality_signals([])
    assert signals["user_msg_count"] == 0
    assert signals["assistant_msg_count"] == 0
    assert signals["avg_response_len"] == 0
    assert signals["vocab_diversity"] == 0.0
    assert signals["has_code"] is False
    assert signals["total_length"] == 0


def test_vocab_diversity_repetitive():
    """Repetitive text should have low diversity."""
    turns = [
        {"role": "user", "content": "the the the the the the the the the the"},
        {"role": "assistant", "content": "the the the the the"},
    ]
    signals = compute_quality_signals(turns)
    # Only one unique word out of many
    assert signals["vocab_diversity"] < 0.2


def test_vocab_diversity_varied():
    """Diverse text should have higher diversity."""
    turns = [
        {"role": "user", "content": "explain quantum computing algorithms"},
        {
            "role": "assistant",
            "content": "quantum computers use qubits for parallel processing",
        },
    ]
    signals = compute_quality_signals(turns)
    assert signals["vocab_diversity"] > 0.5


# --- process_batch ---


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test.db")
    with ConversationStore(db_path) as s:
        yield s


def _insert_exchange(store, request_json, response_json):
    """Helper to insert a raw exchange and return its ID."""
    return store.log_exchange("test-session", request_json, response_json)


def _openai_request(content="What is Python?"):
    return json.dumps({"messages": [{"role": "user", "content": content}]})


def _openai_response(content="Python is a programming language."):
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


def test_process_batch_happy_path(store):
    """Valid exchange gets scrubbed and staged."""
    req = _openai_request("Tell me about programming languages")
    resp = _openai_response("There are many programming languages like Java and Rust.")
    _insert_exchange(store, req, resp)

    scrubber = RegexScrubber()
    count = process_batch(store, scrubber)

    assert count == 1
    staged = store.get_pending_review()
    assert len(staged) == 1
    turns = json.loads(staged[0]["scrubbed_turns"])
    assert len(turns) == 2
    assert turns[0]["role"] == "user"
    assert turns[1]["role"] == "assistant"


def test_process_batch_skips_unparseable(store):
    """Unparseable exchange gets marked skipped."""
    _insert_exchange(store, "not json", "also not json")

    scrubber = RegexScrubber()
    count = process_batch(store, scrubber)

    assert count == 0
    # Should be no staged records
    assert len(store.get_pending_review()) == 0
    # Exchange should be marked skipped
    row = store.conn.execute(
        "SELECT status FROM exchanges WHERE status = 'skipped'"
    ).fetchone()
    assert row is not None


def test_process_batch_skips_short_exchange(store):
    """Exchanges with < 50 chars total content get skipped."""
    req = _openai_request("Hi")
    resp = _openai_response("Hello")
    _insert_exchange(store, req, resp)

    scrubber = RegexScrubber()
    count = process_batch(store, scrubber)

    assert count == 0
    assert len(store.get_pending_review()) == 0


def test_process_batch_content_filter_blocks(store):
    """Content filter blocks harmful content."""
    from common_parlance.filter import KeywordContentFilter

    req = _openai_request(
        "This is a long enough message to pass the length check easily"
    )
    resp = _openai_response("This response is also long enough to pass filters")
    _insert_exchange(store, req, resp)

    scrubber = RegexScrubber()
    cf = KeywordContentFilter()

    # Monkey-patch filter to block everything
    cf.check = lambda text: "test_block"

    count = process_batch(store, scrubber, content_filter=cf)
    assert count == 0
    assert len(store.get_pending_review()) == 0


def test_process_batch_scrubs_pii(store):
    """PII in turns gets replaced with placeholders."""
    req = _openai_request("My email is alice@example.com and phone is 555-123-4567")
    resp = _openai_response(
        "I see your contact info. Let me help you with that request."
    )
    _insert_exchange(store, req, resp)

    scrubber = RegexScrubber()
    count = process_batch(store, scrubber)

    assert count == 1
    staged = store.get_pending_review()
    turns = json.loads(staged[0]["scrubbed_turns"])
    user_content = turns[0]["content"]
    assert "alice@example.com" not in user_content
    assert "[EMAIL]" in user_content


def test_process_batch_sets_metadata(store):
    """Staged record has turn_count, language, quality_signals."""
    req = _openai_request("Tell me about the history of computing in detail")
    resp = _openai_response(
        "Computing has a rich history spanning many decades of innovation."
    )
    _insert_exchange(store, req, resp)

    scrubber = RegexScrubber()
    process_batch(store, scrubber)

    row = store.conn.execute(
        "SELECT turn_count, language, quality_signals FROM staged"
    ).fetchone()
    assert row["turn_count"] == 2
    assert row["language"]  # Should detect some language
    signals = json.loads(row["quality_signals"])
    assert "avg_response_len" in signals
    assert "has_code" in signals


def test_process_batch_multiple_exchanges(store):
    """Processes multiple exchanges in one batch."""
    for i in range(3):
        req = _openai_request(f"Question number {i} about something interesting")
        resp = _openai_response(
            f"Answer number {i} with enough content to pass the filter"
        )
        _insert_exchange(store, req, resp)

    scrubber = RegexScrubber()
    count = process_batch(store, scrubber)

    assert count == 3
    assert len(store.get_pending_review()) == 3


def test_process_batch_empty(store):
    """Returns 0 when no unprocessed exchanges exist."""
    scrubber = RegexScrubber()
    count = process_batch(store, scrubber)
    assert count == 0
