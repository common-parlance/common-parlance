"""SQLite storage for conversation logs."""

import logging
import os
import sqlite3
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class ConversationStore:
    """Local SQLite store for logged conversations."""

    # Schema version history:
    #   0 = fresh database (no tables yet)
    #   1 = initial schema (exchanges + staged tables)
    #   2 = add turn_count and language metadata to staged
    #   3 = add quality_signals JSON blob to staged
    # Bump this and add a _migrate_to_N method for each new version.
    SCHEMA_VERSION = 4

    def __init__(self, path: str = "conversations.db"):
        self.path = Path(path)
        # isolation_level=None for manual transaction control (needed for
        # BEGIN IMMEDIATE in the upload claim pattern).
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._configure_pragmas()
        self._run_migrations()
        self._recover_interrupted_uploads()
        # Set owner-only permissions on DB/WAL/SHM files. Uses chmod
        # (not umask) because umask is process-global and would race
        # with the upload scheduler thread.
        self._secure_file_permissions()

    def _configure_pragmas(self) -> None:
        """Set per-connection pragmas (these don't persist across connections)."""
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA cache_size=-2000")  # ~2MB, plenty for staging
        self.conn.execute("PRAGMA temp_store=MEMORY")
        # Zero deleted data on disk. This database stores raw conversation
        # text (pre-scrubbing) so freed pages must not retain PII.
        self.conn.execute("PRAGMA secure_delete=ON")

    def _get_user_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _set_user_version(self, version: int) -> None:
        self.conn.execute(f"PRAGMA user_version={version}")

    def _run_migrations(self) -> None:
        """Run pending schema migrations based on user_version."""
        current = self._get_user_version()

        migrations = {
            1: self._migrate_to_1,
            2: self._migrate_to_2,
            3: self._migrate_to_3,
            4: self._migrate_to_4,
        }

        for version in sorted(migrations):
            if version > current:
                logger.info("Running DB migration to version %d", version)
                if version == 1:
                    # Migration 1 uses executescript() which auto-commits.
                    # IF NOT EXISTS makes it safe to re-run.
                    # PRAGMA user_version also auto-commits (not DML).
                    migrations[version]()
                    self._set_user_version(version)
                else:
                    # Wrap DDL + version bump in explicit transaction so
                    # a crash can't leave the schema changed but version
                    # not bumped (which would fail on re-run with
                    # "duplicate column").
                    self.conn.execute("BEGIN IMMEDIATE")
                    try:
                        migrations[version]()
                        self._set_user_version(version)
                        self.conn.execute("COMMIT")
                    except Exception:
                        self.conn.execute("ROLLBACK")
                        raise

    def _migrate_to_1(self) -> None:
        """Initial schema: exchanges + staged tables."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS exchanges (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                request_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'raw'
            );

            CREATE TABLE IF NOT EXISTS staged (
                id TEXT PRIMARY KEY,
                exchange_id TEXT NOT NULL REFERENCES exchanges(id),
                created_at TEXT NOT NULL,
                scrubbed_turns TEXT NOT NULL,
                approved INTEGER NOT NULL DEFAULT 0,
                uploaded INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                ner_scrubbed INTEGER NOT NULL DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_exchanges_status
                ON exchanges(status);
            CREATE INDEX IF NOT EXISTS idx_staged_uploaded
                ON staged(uploaded);
            CREATE INDEX IF NOT EXISTS idx_staged_approved
                ON staged(approved);
        """)

    def _migrate_to_2(self) -> None:
        """Add conversation metadata columns to staged table."""
        self.conn.execute(
            "ALTER TABLE staged ADD COLUMN turn_count INTEGER NOT NULL DEFAULT 0"
        )
        self.conn.execute(
            "ALTER TABLE staged ADD COLUMN language TEXT NOT NULL DEFAULT 'en'"
        )

    def _migrate_to_3(self) -> None:
        """Add quality_signals JSON blob to staged table."""
        self.conn.execute(
            "ALTER TABLE staged ADD COLUMN quality_signals TEXT NOT NULL DEFAULT '{}'"
        )

    def _migrate_to_4(self) -> None:
        """Add content_hash column to exchanges for import dedup."""
        self.conn.execute("ALTER TABLE exchanges ADD COLUMN content_hash TEXT")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_exchanges_content_hash "
            "ON exchanges(content_hash) WHERE content_hash IS NOT NULL"
        )

    def _recover_interrupted_uploads(self) -> None:
        """Reset any in-progress uploads from a previous crash.

        uploaded = -1 means "claimed for upload but not yet confirmed."
        On startup, release these claims so they can be retried.
        """
        # isolation_level=None means autocommit — UPDATE commits immediately.
        cursor = self.conn.execute("UPDATE staged SET uploaded = 0 WHERE uploaded = -1")
        if cursor.rowcount:
            logger.info(
                "Recovered %d interrupted upload(s) from previous run",
                cursor.rowcount,
            )

    def log_exchange(
        self,
        session_id: str,
        request_json: str,
        response_json: str,
    ) -> str:
        """Log a raw request/response exchange. Returns the exchange ID."""
        exchange_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO exchanges "
            "(id, session_id, created_at, request_json, response_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (exchange_id, session_id, now, request_json, response_json),
        )
        # Single statement — auto-commits with isolation_level=None
        return exchange_id

    def log_exchange_with_hash(
        self,
        session_id: str,
        request_json: str,
        response_json: str,
        content_hash: str,
    ) -> str | None:
        """Log a raw exchange with dedup hash.

        Returns exchange ID, or None if duplicate.
        """
        exchange_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        try:
            self.conn.execute(
                "INSERT INTO exchanges "
                "(id, session_id, created_at, request_json,"
                " response_json, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    exchange_id,
                    session_id,
                    now,
                    request_json,
                    response_json,
                    content_hash,
                ),
            )
        except sqlite3.IntegrityError:
            return None
        return exchange_id

    def mark_skipped(self, exchange_id: str) -> None:
        """Mark an exchange as skipped (unparseable, too short, or filtered)."""
        self.conn.execute(
            "UPDATE exchanges SET status = 'skipped' WHERE id = ?",
            (exchange_id,),
        )

    def get_unprocessed(self, limit: int = 100) -> list[sqlite3.Row]:
        """Get exchanges that haven't been PII-scrubbed yet."""
        cursor = self.conn.execute(
            "SELECT id, request_json, response_json FROM exchanges "
            "WHERE status = 'raw' ORDER BY created_at LIMIT ?",
            (limit,),
        )
        return cursor.fetchall()

    def mark_processed(
        self,
        exchange_id: str,
        scrubbed_turns: str,
        *,
        ner_scrubbed: bool = True,
        turn_count: int = 0,
        language: str = "en",
        quality_signals: str = "{}",
    ) -> str:
        """Move a processed exchange to the staging table."""
        staged_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        self.conn.execute("BEGIN")
        self.conn.execute(
            "INSERT INTO staged "
            "(id, exchange_id, created_at, scrubbed_turns, ner_scrubbed, "
            "turn_count, language, quality_signals) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                staged_id,
                exchange_id,
                now,
                scrubbed_turns,
                int(ner_scrubbed),
                turn_count,
                language,
                quality_signals,
            ),
        )
        self.conn.execute(
            "UPDATE exchanges SET status = 'processed' WHERE id = ?",
            (exchange_id,),
        )
        self.conn.execute("COMMIT")
        return staged_id

    def get_pending_review(self, limit: int = 50) -> list[sqlite3.Row]:
        """Get staged conversations awaiting user approval."""
        cursor = self.conn.execute(
            "SELECT id, scrubbed_turns, created_at FROM staged "
            "WHERE approved = 0 AND uploaded = 0 ORDER BY created_at LIMIT ?",
            (limit,),
        )
        return cursor.fetchall()

    def approve(self, staged_id: str) -> None:
        """Approve a staged conversation for upload."""
        self.conn.execute("UPDATE staged SET approved = 1 WHERE id = ?", (staged_id,))

    def approve_batch(self, staged_ids: list[str]) -> None:
        """Approve multiple staged conversations in one transaction."""
        self.conn.execute("BEGIN")
        self.conn.executemany(
            "UPDATE staged SET approved = 1 WHERE id = ?",
            [(sid,) for sid in staged_ids],
        )
        self.conn.execute("COMMIT")

    def unapprove_all(self) -> int:
        """Reset all approved-but-not-uploaded conversations back to pending review."""
        cursor = self.conn.execute(
            "UPDATE staged SET approved = 0 WHERE approved = 1 AND uploaded = 0"
        )
        return cursor.rowcount

    def update_scrubbed_turns(self, staged_id: str, scrubbed_turns: str) -> None:
        """Update the scrubbed turns for a staged conversation (manual redaction)."""
        self.conn.execute(
            "UPDATE staged SET scrubbed_turns = ? WHERE id = ?",
            (scrubbed_turns, staged_id),
        )

    def reject(self, staged_id: str) -> None:
        """Reject a staged conversation — deletes it from staging."""
        self.conn.execute("DELETE FROM staged WHERE id = ?", (staged_id,))

    # Conversations that fail server-side validation this many times
    # are set aside ("dead-lettered") and skipped in future uploads.
    MAX_UPLOAD_FAILURES = 3

    def get_ready_for_upload(self, limit: int = 100) -> list[sqlite3.Row]:
        """Claim and return approved conversations ready for upload.

        Uses BEGIN IMMEDIATE to prevent concurrent uploaders (scheduler
        thread + CLI) from claiming the same rows. Claimed rows are
        marked uploaded = -1 (in-progress). Call mark_uploaded() on
        success or release_upload_claim() on failure.

        Server-side NER handles name scrubbing, so ner_scrubbed is
        informational only (not a gate).
        Skips dead-lettered rows (fail_count >= MAX_UPLOAD_FAILURES).
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # First, find eligible row IDs under the lock
            cursor = self.conn.execute(
                "SELECT id FROM staged "
                "WHERE approved = 1 AND uploaded = 0 "
                "AND fail_count < ? "
                "ORDER BY created_at LIMIT ?",
                (self.MAX_UPLOAD_FAILURES, limit),
            )
            ids = [row[0] for row in cursor.fetchall()]
            if not ids:
                self.conn.execute("COMMIT")
                return []

            # Claim those specific rows
            placeholders = ",".join("?" * len(ids))
            self.conn.execute(
                f"UPDATE staged SET uploaded = -1 "  # noqa: S608
                f"WHERE id IN ({placeholders})",
                ids,
            )

            # Read the claimed rows with full data
            cursor = self.conn.execute(
                f"SELECT id, scrubbed_turns, turn_count, language, "  # noqa: S608
                f"quality_signals, ner_scrubbed FROM staged "
                f"WHERE id IN ({placeholders}) "
                f"ORDER BY created_at",
                ids,
            )
            rows = cursor.fetchall()
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return rows

    def mark_uploaded(self, staged_ids: list[str]) -> None:
        """Mark claimed conversations as successfully uploaded."""
        self.conn.execute("BEGIN")
        self.conn.executemany(
            "UPDATE staged SET uploaded = 1 WHERE id = ?",
            [(sid,) for sid in staged_ids],
        )
        self.conn.execute("COMMIT")

    def release_upload_claim(self, staged_ids: list[str]) -> None:
        """Release claimed rows back to uploaded = 0 after a failed upload."""
        self.conn.execute("BEGIN")
        self.conn.executemany(
            "UPDATE staged SET uploaded = 0 WHERE id = ? AND uploaded = -1",
            [(sid,) for sid in staged_ids],
        )
        self.conn.execute("COMMIT")

    def increment_fail_count(self, staged_ids: list[str]) -> None:
        """Increment failure count and release upload claim for failed conversations."""
        self.conn.execute("BEGIN")
        self.conn.executemany(
            "UPDATE staged SET fail_count = fail_count + 1, uploaded = 0 WHERE id = ?",
            [(sid,) for sid in staged_ids],
        )
        self.conn.execute("COMMIT")
        # Log any newly dead-lettered conversations
        placeholders = ",".join("?" * len(staged_ids))
        dead = self.conn.execute(
            f"SELECT COUNT(*) FROM staged "  # noqa: S608
            f"WHERE id IN ({placeholders}) AND fail_count >= ?",
            [*staged_ids, self.MAX_UPLOAD_FAILURES],
        ).fetchone()[0]
        if dead:
            logger.warning(
                "%d conversation(s) dead-lettered after %d failures",
                dead,
                self.MAX_UPLOAD_FAILURES,
            )

    def stats(self) -> dict:
        """Get counts for each stage of the pipeline."""
        ex = self.conn.execute(
            "SELECT "
            "SUM(CASE WHEN status = 'raw' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END) "
            "FROM exchanges"
        ).fetchone()
        st = self.conn.execute(
            "SELECT "
            "SUM(CASE WHEN approved = 0 AND uploaded = 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN approved = 1 AND uploaded = 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN uploaded = 1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN fail_count >= ? AND uploaded = 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN ner_scrubbed = 0 AND uploaded = 0 THEN 1 ELSE 0 END) "
            "FROM staged",
            (self.MAX_UPLOAD_FAILURES,),
        ).fetchone()
        return {
            "raw": ex[0] or 0,
            "processed": ex[1] or 0,
            "pending_review": st[0] or 0,
            "approved": st[1] or 0,
            "uploaded": st[2] or 0,
            "dead_lettered": st[3] or 0,
            "no_ner": st[4] or 0,
        }

    def get_dead_letters(self, limit: int = 50) -> list[sqlite3.Row]:
        """Get conversations that have been dead-lettered after repeated failures."""
        cursor = self.conn.execute(
            "SELECT id, created_at, fail_count, scrubbed_turns FROM staged "
            "WHERE fail_count >= ? AND uploaded = 0 "
            "ORDER BY created_at LIMIT ?",
            (self.MAX_UPLOAD_FAILURES, limit),
        )
        return cursor.fetchall()

    def retry_dead_letters(self) -> int:
        """Reset fail_count on all dead-lettered conversations so they retry."""
        cursor = self.conn.execute(
            "UPDATE staged SET fail_count = 0 WHERE fail_count >= ? AND uploaded = 0",
            (self.MAX_UPLOAD_FAILURES,),
        )
        return cursor.rowcount

    def _secure_file_permissions(self) -> None:
        """Set owner-only permissions on the database and WAL/SHM files."""
        owner_only = stat.S_IRUSR | stat.S_IWUSR
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                os.chmod(str(p), owner_only)

    def purge_processed_raw(self) -> int:
        """Delete raw exchange data for conversations that have been uploaded.

        Keeps the scrubbed staged records but removes the original raw
        request/response JSON that contains PII, system prompts, and
        model metadata. Returns the number of exchanges cleaned.
        """
        cursor = self.conn.execute(
            "SELECT e.id FROM exchanges e "
            "JOIN staged s ON s.exchange_id = e.id "
            "WHERE s.uploaded = 1 AND e.status = 'processed'"
        )
        ids = [row[0] for row in cursor.fetchall()]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        # Temporarily disable FK checks to delete the parent exchange
        # while keeping the orphaned staged record (scrubbed, PII-free).
        # PRAGMA foreign_keys is per-connection (not persistent), so a crash
        # here won't affect future connections — _configure_pragmas re-enables it.
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("BEGIN")
            self.conn.execute(
                f"DELETE FROM exchanges WHERE id IN ({placeholders})",  # noqa: S608
                ids,
            )
            self.conn.execute("COMMIT")
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")
        # secure_delete=ON zeros freed pages in the main DB file, but NOT
        # in the WAL. Checkpoint + VACUUM + checkpoint ensures deleted PII
        # is removed from all on-disk files.
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return len(ids)

    def purge_all(self) -> dict[str, int]:
        """Delete all local data (raw exchanges and staged conversations).

        Used when consent is revoked — removes everything that hasn't
        been uploaded yet. Already-uploaded anonymous data on HuggingFace
        cannot be recalled.
        """
        staged_count = self.conn.execute("SELECT COUNT(*) FROM staged").fetchone()[0]
        exchange_count = self.conn.execute("SELECT COUNT(*) FROM exchanges").fetchone()[
            0
        ]
        self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM staged")
        self.conn.execute("DELETE FROM exchanges")
        self.conn.execute("COMMIT")
        # secure_delete=ON zeros freed pages in the main DB file, but NOT
        # in the WAL. Checkpoint + VACUUM + checkpoint ensures deleted PII
        # is removed from all on-disk files.
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {"exchanges": exchange_count, "staged": staged_count}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self) -> None:
        self.conn.close()
