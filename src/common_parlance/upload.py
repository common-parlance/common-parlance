"""Upload approved conversations via the Common Parlance proxy.

Uploads go through our Cloudflare Worker (auth proxy) which validates
the API key, runs server-side content checks, and forwards to HuggingFace.
No HuggingFace credentials needed on the client side.
"""

import gzip
import json
import logging
import random
import threading
import time
import uuid

import httpx

from common_parlance.config import DEFAULT_CONFIG

logger = logging.getLogger(__name__)

DEFAULT_PROXY_URL = DEFAULT_CONFIG["proxy_url"]
DEFAULT_INTERVAL_HOURS = 24
MAX_RETRIES = 5
# Keep chunks small so server-side NER can process all turns within the
# Worker's 30s wall-clock limit. 256KB ≈ ~100-150 turns, well within the
# NER service's capacity. Data isn't urgent — smaller chunks are safer.
MAX_BATCH_BYTES = 256 * 1024  # 256KB per chunk
# Delay between chunks to be gentle on the Worker and HuggingFace
CHUNK_DELAY_SECONDS = 2.0


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter."""
    base = min(2**attempt, 300)  # cap at 5 minutes
    return base + random.uniform(0, base * 0.5)


def _chunk_rows(rows: list, max_bytes: int = MAX_BATCH_BYTES) -> list[list]:
    """Split rows into chunks that fit within the byte limit.

    Each chunk produces a JSONL payload under max_bytes.
    """
    chunks: list[list] = []
    current_chunk: list = []
    current_size = 0

    for row in rows:
        try:
            turns = json.loads(row["scrubbed_turns"])
            signals = json.loads(row["quality_signals"])
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Skipping row with corrupt JSON: %s",
                row.get("id", "unknown"),
            )
            continue
        record = {
            "conversation_id": str(uuid.uuid4()),
            "turns": turns,
            "turn_count": row["turn_count"],
            "language": row["language"],
            "quality_signals": signals,
            "ner_scrubbed": bool(row["ner_scrubbed"]),
        }
        line = json.dumps(record, ensure_ascii=False)
        line_size = len(line.encode("utf-8")) + 1  # +1 for newline

        if current_chunk and current_size + line_size > max_bytes:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0

        current_chunk.append((row, line))
        current_size += line_size

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _upload_one_chunk(
    chunk: list[tuple],
    proxy_url: str,
    api_key: str,
) -> str:
    """Upload a single chunk.

    Returns:
        "ok" on success, "rejected" on content/validation rejection (422),
        "auth" on auth failure, "error" on transient failure.
    """
    jsonl_content = "\n".join(line for _, line in chunk)
    compressed = gzip.compress(jsonl_content.encode("utf-8"), compresslevel=1)

    for attempt in range(MAX_RETRIES):
        try:
            response = httpx.post(
                f"{proxy_url.rstrip('/')}/upload",
                content=compressed,
                headers={
                    "X-API-Key": api_key,
                    "Content-Type": "application/x-ndjson",
                    "Content-Encoding": "gzip",
                },
                timeout=60.0,
            )

            if response.status_code == 401:
                logger.error("Upload rejected: invalid API key.")
                return "auth"

            if response.status_code == 422:
                try:
                    body = response.json()
                    msg = body.get("error", "unknown")
                except Exception:
                    msg = response.text[:200]
                logger.error("Upload rejected: %s", msg)
                return "rejected"

            if response.status_code == 429:
                try:
                    body = response.json()
                    msg = body.get("error", "")
                except Exception:
                    msg = ""
                if "cooldown" in msg.lower():
                    logger.error(
                        "Upload blocked: %s\n"
                        "New API keys have a 1-hour cooldown."
                        " Try again later.",
                        msg,
                    )
                    return "rate_limited"
                delay = _backoff_delay(attempt)
                logger.warning(
                    "Rate limited, retrying in %.1fs",
                    delay,
                )
                time.sleep(delay)
                continue

            response.raise_for_status()
            return "ok"

        except httpx.HTTPStatusError:
            if response.status_code == 503:
                try:
                    body = response.json()
                    msg = body.get("error", "")
                except Exception:
                    msg = ""
                if "NER" in msg or "unavailable" in msg.lower():
                    logger.error(
                        "Upload blocked: NER service is unavailable.\n"
                        "The NER service scrubs names and locations from your data.\n"
                        "It may be starting up. Please wait a minute and try again."
                    )
                    return "ner_unavailable"
            if attempt < MAX_RETRIES - 1:
                delay = _backoff_delay(attempt)
                logger.warning(
                    "Upload attempt %d/%d failed (HTTP %d), retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    response.status_code,
                    delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Upload failed after %d attempts.",
                    MAX_RETRIES,
                )
                return "error"

        except (
            httpx.ConnectError,
            httpx.TimeoutException,
        ):
            if attempt < MAX_RETRIES - 1:
                delay = _backoff_delay(attempt)
                logger.warning(
                    "Upload attempt %d/%d failed (connection error), retrying in %.1fs",
                    attempt + 1,
                    MAX_RETRIES,
                    delay,
                    exc_info=True,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Upload failed after %d attempts.",
                    MAX_RETRIES,
                    exc_info=True,
                )
                return "error"

    return "error"


def upload_batch(
    store,
    proxy_url: str = DEFAULT_PROXY_URL,
    api_key: str = "",
    limit: int = 500,
) -> int:
    """Upload approved conversations via the proxy, chunking if needed.

    Splits large backlogs into multiple requests to stay under the
    Worker's size limit. Returns the total number of conversations uploaded.
    """
    if not api_key:
        logger.error("No API key configured. Register with: common-parlance register")
        return 0

    rows = store.get_ready_for_upload(limit=limit)
    if not rows:
        logger.info("No conversations ready for upload.")
        return 0

    # Track all claimed row IDs so we can release unclaimed ones on exit
    all_claimed_ids = [row["id"] for row in rows]

    chunks = _chunk_rows(rows)
    logger.info(
        "Uploading %d conversations via %s (%d chunk%s)",
        len(rows),
        proxy_url,
        len(chunks),
        "s" if len(chunks) > 1 else "",
    )

    total_uploaded = 0
    uploaded_ids = []
    failed_ids = []

    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            logger.info(
                "Uploading chunk %d/%d (%d conversations)",
                i + 1,
                len(chunks),
                len(chunk),
            )

        # Delay between chunks to stay gentle on free tier limits
        if i > 0:
            time.sleep(CHUNK_DELAY_SECONDS)

        result = _upload_one_chunk(chunk, proxy_url, api_key)
        chunk_ids = [row["id"] for row, _ in chunk]

        if result == "ok":
            store.mark_uploaded(chunk_ids)
            uploaded_ids.extend(chunk_ids)
            total_uploaded += len(chunk)
        elif result == "rejected":
            # Chunk rejected — isolate the bad conversation(s) by
            # retrying each one individually so good ones aren't
            # collateral-damaged by a single bad record.
            if len(chunk) > 1:
                aborted = False
                for row, line in chunk:
                    single = [(row, line)]
                    single_result = _upload_one_chunk(single, proxy_url, api_key)
                    if single_result == "ok":
                        store.mark_uploaded([row["id"]])
                        uploaded_ids.append(row["id"])
                        total_uploaded += 1
                    elif single_result == "rejected":
                        store.increment_fail_count([row["id"]])
                        failed_ids.append(row["id"])
                    # auth/error: stop entirely
                    elif single_result == "auth":
                        aborted = True
                        break
                    else:
                        aborted = True
                        break
                if aborted:
                    logger.warning(
                        "Chunk %d/%d: isolation aborted (auth/transient error).",
                        i + 1,
                        len(chunks),
                    )
                else:
                    logger.warning(
                        "Chunk %d/%d rejected by server, isolated bad records.",
                        i + 1,
                        len(chunks),
                    )
            else:
                store.increment_fail_count(chunk_ids)
                logger.warning(
                    "Chunk %d/%d rejected by server (single record).",
                    i + 1,
                    len(chunks),
                )
        elif result == "auth":
            logger.error("Stopping upload — fix API key before retrying.")
            break
        elif result == "ner_unavailable":
            logger.error("Stopping upload — NER service must be available.")
            break
        elif result == "rate_limited":
            logger.error("Stopping upload — rate limit reached.")
            break
        else:
            logger.error(
                "Chunk %d/%d failed. Remaining conversations "
                "preserved locally — will retry next run.",
                i + 1,
                len(chunks),
            )
            break  # Stop on transient failure, retry next run

    # Release any rows still claimed (uploaded = -1) that weren't
    # handled above (e.g., remaining chunks after an auth/error break)
    handled_ids = set(uploaded_ids) | set(failed_ids)
    unclaimed_ids = [rid for rid in all_claimed_ids if rid not in handled_ids]
    if unclaimed_ids:
        store.release_upload_claim(unclaimed_ids)

    # Clean up raw PII data for successfully uploaded conversations
    cleaned = store.purge_processed_raw()
    if cleaned:
        logger.info("Cleaned %d raw exchanges (PII data removed)", cleaned)

    if total_uploaded:
        logger.info("Successfully uploaded %d conversations.", total_uploaded)

    return total_uploaded


class UploadScheduler:
    """Background thread that uploads approved conversations on a schedule."""

    def __init__(
        self,
        db_path: str = "conversations.db",
        proxy_url: str = DEFAULT_PROXY_URL,
        api_key: str = "",
        interval_hours: float = DEFAULT_INTERVAL_HOURS,
    ):
        self._db_path = db_path
        self._proxy_url = proxy_url
        self._api_key = api_key
        self._interval = interval_hours * 3600
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background upload scheduler."""
        if self._thread is not None:
            return

        if not self._api_key:
            logger.warning(
                "Upload scheduler not started: no API key configured. "
                "Register with: common-parlance register"
            )
            return

        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="upload-scheduler"
        )
        self._thread.start()
        logger.info(
            "Upload scheduler started (every %.1f hours via %s)",
            self._interval / 3600,
            self._proxy_url,
        )

    def stop(self) -> None:
        """Signal the scheduler to stop."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run_loop(self) -> None:
        """Main scheduler loop.

        Uses wall-clock time to detect sleep/wake — if the OS suspended
        the process (laptop lid close), we upload soon after wake instead
        of waiting the full remaining interval.
        """
        from common_parlance.consent import has_consent
        from common_parlance.db import ConversationStore

        # Wait a bit on startup before first check
        if self._stop_event.wait(60):
            return

        last_run = time.monotonic()

        while not self._stop_event.is_set():
            # Re-check consent before each upload cycle — if the user
            # revoked consent (e.g., via CLI in another terminal), stop
            # uploading immediately rather than racing with purge.
            if not has_consent():
                logger.info("Consent revoked — upload scheduler stopping")
                return

            try:
                with ConversationStore(self._db_path) as store:
                    upload_batch(
                        store,
                        proxy_url=self._proxy_url,
                        api_key=self._api_key,
                    )
            except Exception as exc:
                # Log type only — exc_info traceback could leak conversation PII
                logger.error("Upload scheduler error: %s", type(exc).__name__)

            last_run = time.monotonic()

            # Sleep in short intervals to detect wake-from-sleep.
            # If monotonic clock jumps (sleep happened), run immediately.
            while not self._stop_event.is_set():
                before = time.monotonic()
                if self._stop_event.wait(min(self._interval, 300)):
                    return
                after = time.monotonic()
                elapsed_since_run = after - last_run

                # If enough time has passed (including sleep), break to upload
                if elapsed_since_run >= self._interval:
                    break

                # Detect large clock jump (sleep/wake) — if we slept through
                # more than the interval, upload on next iteration
                wall_jump = after - before
                if wall_jump > 600 and elapsed_since_run >= self._interval * 0.5:
                    logger.info(
                        "Detected wake from sleep (%.0fs gap), uploading early",
                        wall_jump,
                    )
                    break
