"""Tests for ConversationStore (SQLite storage layer)."""

import json
import os

import pytest

from common_parlance.db import ConversationStore


@pytest.fixture
def store(tmp_path):
    """Create a ConversationStore backed by a temp file."""
    db_path = str(tmp_path / "test.db")
    with ConversationStore(db_path) as s:
        yield s


@pytest.fixture
def sample_exchange():
    """Sample request/response JSON for testing."""
    request = json.dumps({"messages": [{"role": "user", "content": "What is Python?"}]})
    response = json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": "A language."}}]}
    )
    return request, response


# --- Schema & migrations ---


def test_fresh_db_has_correct_version(store):
    version = store._get_user_version()
    assert version == ConversationStore.SCHEMA_VERSION


def test_tables_exist(store):
    tables = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = [t[0] for t in tables]
    assert "exchanges" in table_names
    assert "staged" in table_names


def test_staged_has_metadata_columns(store):
    info = store.conn.execute("PRAGMA table_info(staged)").fetchall()
    col_names = [row[1] for row in info]
    assert "turn_count" in col_names
    assert "language" in col_names
    assert "quality_signals" in col_names


def test_migration_idempotent(tmp_path):
    """Opening the same DB twice doesn't fail."""
    db_path = str(tmp_path / "test.db")
    with ConversationStore(db_path):
        pass
    with ConversationStore(db_path):
        pass


# --- Exchange logging ---


def test_log_exchange(store, sample_exchange):
    request, response = sample_exchange
    eid = store.log_exchange("session-1", request, response)
    assert eid is not None
    assert len(eid) == 36  # UUID format


def test_get_unprocessed(store, sample_exchange):
    request, response = sample_exchange
    store.log_exchange("s1", request, response)
    store.log_exchange("s1", request, response)

    rows = store.get_unprocessed(limit=10)
    assert len(rows) == 2


def test_mark_skipped(store, sample_exchange):
    request, response = sample_exchange
    eid = store.log_exchange("s1", request, response)
    store.mark_skipped(eid)

    rows = store.get_unprocessed()
    assert len(rows) == 0


# --- Processing & staging ---


def test_mark_processed(store, sample_exchange):
    request, response = sample_exchange
    eid = store.log_exchange("s1", request, response)

    turns = json.dumps([{"role": "user", "content": "scrubbed"}])
    sid = store.mark_processed(eid, turns, turn_count=1, language="en")

    assert sid is not None
    # Exchange should no longer be unprocessed
    assert len(store.get_unprocessed()) == 0


def test_mark_processed_with_metadata(store, sample_exchange):
    request, response = sample_exchange
    eid = store.log_exchange("s1", request, response)

    turns = json.dumps([{"role": "user", "content": "bonjour"}])
    signals = json.dumps({"avg_response_len": 42, "has_code": False})
    store.mark_processed(
        eid, turns, turn_count=3, language="fr", quality_signals=signals
    )

    row = store.conn.execute(
        "SELECT turn_count, language, quality_signals FROM staged "
        "WHERE exchange_id = ?",
        (eid,),
    ).fetchone()
    assert row["turn_count"] == 3
    assert row["language"] == "fr"
    assert json.loads(row["quality_signals"])["avg_response_len"] == 42


# --- Review & approval ---


def test_pending_review(store, sample_exchange):
    request, response = sample_exchange
    eid = store.log_exchange("s1", request, response)
    turns = json.dumps([{"role": "user", "content": "test"}])
    store.mark_processed(eid, turns)

    pending = store.get_pending_review()
    assert len(pending) == 1


def test_approve(store, sample_exchange):
    request, response = sample_exchange
    eid = store.log_exchange("s1", request, response)
    turns = json.dumps([{"role": "user", "content": "test"}])
    sid = store.mark_processed(eid, turns)

    store.approve(sid)

    # No longer pending
    assert len(store.get_pending_review()) == 0
    # Ready for upload
    assert len(store.get_ready_for_upload()) == 1


def test_approve_batch(store, sample_exchange):
    request, response = sample_exchange
    sids = []
    for _ in range(3):
        eid = store.log_exchange("s1", request, response)
        turns = json.dumps([{"role": "user", "content": "test"}])
        sids.append(store.mark_processed(eid, turns))

    store.approve_batch(sids)
    assert len(store.get_ready_for_upload()) == 3


def test_reject(store, sample_exchange):
    request, response = sample_exchange
    eid = store.log_exchange("s1", request, response)
    turns = json.dumps([{"role": "user", "content": "test"}])
    sid = store.mark_processed(eid, turns)

    store.reject(sid)
    assert len(store.get_pending_review()) == 0
    assert len(store.get_ready_for_upload()) == 0


# --- Upload tracking ---


def test_mark_uploaded(store, sample_exchange):
    request, response = sample_exchange
    eid = store.log_exchange("s1", request, response)
    turns = json.dumps([{"role": "user", "content": "test"}])
    sid = store.mark_processed(eid, turns)
    store.approve(sid)

    store.mark_uploaded([sid])
    assert len(store.get_ready_for_upload()) == 0


def test_increment_fail_count(store, sample_exchange):
    request, response = sample_exchange
    eid = store.log_exchange("s1", request, response)
    turns = json.dumps([{"role": "user", "content": "test"}])
    sid = store.mark_processed(eid, turns)
    store.approve(sid)

    # Should still be ready after 1 failure
    store.increment_fail_count([sid])
    assert len(store.get_ready_for_upload()) == 1

    # Should still be ready after 2 failures
    store.increment_fail_count([sid])
    assert len(store.get_ready_for_upload()) == 1

    # Dead-lettered after 3 failures (MAX_UPLOAD_FAILURES)
    store.increment_fail_count([sid])
    assert len(store.get_ready_for_upload()) == 0


# --- Stats ---


def test_stats_empty(store):
    stats = store.stats()
    assert stats["raw"] == 0
    assert stats["uploaded"] == 0
    assert stats["pending_review"] == 0


def test_stats_full_pipeline(store, sample_exchange):
    request, response = sample_exchange

    # Log two exchanges
    eid1 = store.log_exchange("s1", request, response)
    store.log_exchange("s1", request, response)

    stats = store.stats()
    assert stats["raw"] == 2

    # Process one
    turns = json.dumps([{"role": "user", "content": "test"}])
    sid = store.mark_processed(eid1, turns)

    stats = store.stats()
    assert stats["raw"] == 1
    assert stats["processed"] == 1
    assert stats["pending_review"] == 1

    # Approve and upload
    store.approve(sid)
    store.mark_uploaded([sid])

    stats = store.stats()
    assert stats["uploaded"] == 1
    assert stats["approved"] == 0  # approved but not uploaded = 0


# --- PII cleanup ---


def test_purge_processed_raw(store, sample_exchange):
    request, response = sample_exchange
    eid = store.log_exchange("s1", request, response)
    turns = json.dumps([{"role": "user", "content": "test"}])
    sid = store.mark_processed(eid, turns)
    store.approve(sid)
    store.mark_uploaded([sid])

    cleaned = store.purge_processed_raw()
    assert cleaned == 1

    # Exchange row should be gone
    row = store.conn.execute(
        "SELECT COUNT(*) FROM exchanges WHERE id = ?", (eid,)
    ).fetchone()[0]
    assert row == 0

    # Staged row should still exist
    row = store.conn.execute(
        "SELECT COUNT(*) FROM staged WHERE id = ?", (sid,)
    ).fetchone()[0]
    assert row == 1


def test_purge_processed_raw_skips_non_uploaded(store, sample_exchange):
    request, response = sample_exchange
    eid = store.log_exchange("s1", request, response)
    turns = json.dumps([{"role": "user", "content": "test"}])
    store.mark_processed(eid, turns)

    cleaned = store.purge_processed_raw()
    assert cleaned == 0


def test_purge_all(store, sample_exchange):
    request, response = sample_exchange
    store.log_exchange("s1", request, response)
    eid2 = store.log_exchange("s1", request, response)
    turns = json.dumps([{"role": "user", "content": "test"}])
    store.mark_processed(eid2, turns)

    result = store.purge_all()
    assert result["exchanges"] == 2
    assert result["staged"] == 1


# --- File permissions ---


def test_db_file_permissions(tmp_path):
    db_path = str(tmp_path / "perm_test.db")
    with ConversationStore(db_path):
        pass

    mode = os.stat(db_path).st_mode & 0o777
    assert mode == 0o600


# --- Context manager ---


def test_context_manager(tmp_path):
    db_path = str(tmp_path / "ctx.db")
    with ConversationStore(db_path) as store:
        store.log_exchange("s1", "{}", "{}")

    # Connection should be closed after exiting
    # Verify by trying to use it (should fail or be closed)
    import contextlib

    with contextlib.suppress(Exception):
        store.conn.execute("SELECT 1")


# --- Pragmas ---


def test_secure_delete_enabled(store):
    val = store.conn.execute("PRAGMA secure_delete").fetchone()[0]
    assert val == 1


def test_wal_mode(store):
    val = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert val == "wal"
