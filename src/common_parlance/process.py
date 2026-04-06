"""Batch processor: reads raw exchanges, scrubs PII, stages for review."""

import json
import logging
import re

from common_parlance.db import ConversationStore
from common_parlance.extract import extract_turns
from common_parlance.filter import ContentFilter
from common_parlance.lang import detect_language
from common_parlance.scrub import Scrubber

logger = logging.getLogger(__name__)

# Matches fenced code blocks (```...```)
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")


def compute_quality_signals(turns: list[dict]) -> dict:
    """Compute rule-based quality heuristics for a conversation.

    Returns a dict of signals that downstream consumers can use to filter
    or rank conversations. All signals are non-identifying metadata.
    """
    user_turns = [t for t in turns if t["role"] == "user"]
    assistant_turns = [t for t in turns if t["role"] == "assistant"]

    # Average response length (assistant turns only)
    assistant_lengths = [len(t["content"]) for t in assistant_turns]
    avg_response_len = (
        sum(assistant_lengths) / len(assistant_lengths) if assistant_lengths else 0
    )

    # Has code blocks
    all_content = " ".join(t["content"] for t in turns)
    has_code = bool(_CODE_BLOCK_RE.search(all_content))

    # Vocabulary diversity: type-token ratio on lowercased words.
    # Higher = more diverse vocabulary. Capped at 1.0.
    words = all_content.lower().split()
    vocab_diversity = round(len(set(words)) / len(words), 3) if words else 0.0

    # Total character count across all turns
    total_length = sum(len(t["content"]) for t in turns)

    # User message count and assistant message count
    user_msg_count = len(user_turns)
    assistant_msg_count = len(assistant_turns)

    return {
        "avg_response_len": round(avg_response_len),
        "has_code": has_code,
        "vocab_diversity": vocab_diversity,
        "total_length": total_length,
        "user_msg_count": user_msg_count,
        "assistant_msg_count": assistant_msg_count,
    }


def process_batch(
    store: ConversationStore,
    scrubber: Scrubber,
    limit: int = 100,
    content_filter: ContentFilter | None = None,
) -> int:
    """Process a batch of raw exchanges through PII scrubbing.

    Returns the number of exchanges successfully processed.
    """
    exchanges = store.get_unprocessed(limit=limit)
    if not exchanges:
        return 0

    processed = 0
    filtered = 0
    for exchange in exchanges:
        exchange_id = exchange["id"]

        try:
            turns = extract_turns(exchange["request_json"], exchange["response_json"])
        except Exception:
            logger.warning(
                "Error extracting turns from exchange %s",
                exchange_id,
                exc_info=True,
            )
            store.mark_skipped(exchange_id)
            continue

        if turns is None:
            logger.warning("Could not extract turns from exchange %s", exchange_id)
            store.mark_skipped(exchange_id)
            continue

        # Filter out very short exchanges (not useful data).
        total_content = sum(len(t.get("content", "")) for t in turns)
        if total_content < 50:
            logger.debug(
                "Skipping short exchange %s (%d chars)",
                exchange_id,
                total_content,
            )
            store.mark_skipped(exchange_id)
            continue

        # Content filter: check all turns for harmful content.
        if content_filter is not None:
            blocked = False
            for turn in turns:
                reason = content_filter.check(turn["content"])
                if reason is not None:
                    logger.info(
                        "Exchange %s blocked by content filter: %s",
                        exchange_id,
                        reason,
                    )
                    blocked = True
                    filtered += 1
                    break
            if blocked:
                store.mark_skipped(exchange_id)
                continue

        try:
            # Detect language from concatenated turn content.
            all_content = " ".join(t.get("content", "") for t in turns)
            language = detect_language(all_content)

            # Scrub PII from each turn's content.
            scrubbed_turns = [
                {"role": turn["role"], "content": scrubber.scrub(turn["content"])}
                for turn in turns
            ]

            # Compute quality signals from scrubbed content.
            signals = compute_quality_signals(scrubbed_turns)

            # Stage the scrubbed conversation.
            scrubbed_json = json.dumps(scrubbed_turns)
            store.mark_processed(
                exchange_id,
                scrubbed_json,
                ner_scrubbed=scrubber.has_ner,
                turn_count=len(turns),
                language=language,
                quality_signals=json.dumps(signals),
            )
            processed += 1
        except Exception:
            logger.warning("Error processing exchange %s", exchange_id, exc_info=True)
            store.mark_skipped(exchange_id)

    if filtered:
        logger.info("Content filter blocked %d exchanges", filtered)
    logger.info("Processed %d/%d exchanges", processed, len(exchanges))
    return processed
