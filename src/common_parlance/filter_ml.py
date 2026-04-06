"""Optional ML-based content filter using Detoxify.

Detoxify runs a DistilBERT model (~250MB) that classifies text across six
toxicity dimensions. This is a heavier filter than the keyword blocklist —
it catches contextual toxicity that regexes miss, but adds a PyTorch dependency.

Install: uv pip install detoxify

This module is imported lazily — if detoxify is not installed, the keyword
filter continues to work. No PII is stored or logged by this filter.
"""

import logging

logger = logging.getLogger(__name__)

# Default thresholds per category. Tuned conservatively — prefer false
# negatives (let borderline content through to human review) over false
# positives (blocking legitimate conversations).
DEFAULT_THRESHOLDS = {
    "toxicity": 0.85,
    "severe_toxicity": 0.70,
    "obscene": 0.90,
    "threat": 0.80,
    "insult": 0.90,
    "identity_attack": 0.80,
    "sexual_explicit": 0.85,
}


class DetoxifyContentFilter:
    """ML-based content filter using Detoxify (DistilBERT).

    Classifies text across toxicity dimensions. Returns the category name
    if any score exceeds its threshold, or None if safe.

    No conversation content is logged — only the category and score of
    blocked content (for threshold tuning).
    """

    def __init__(
        self,
        thresholds: dict[str, float] | None = None,
        model_type: str = "original",
    ) -> None:
        try:
            from detoxify import Detoxify
        except ImportError:
            raise ImportError(
                "Detoxify is not installed. Install it with: uv pip install detoxify"
            ) from None

        self._model = Detoxify(model_type)
        self._thresholds = thresholds or DEFAULT_THRESHOLDS
        logger.info("Detoxify content filter initialized (model=%s)", model_type)

    def check(self, text: str) -> str | None:
        """Check text for toxicity using ML classification.

        Returns None if safe, or the toxicity category if blocked.
        No content is logged — only category and score.
        """
        if not text.strip():
            return None

        results = self._model.predict(text)

        for category, threshold in self._thresholds.items():
            score = results.get(category, 0.0)
            if score >= threshold:
                # Log category and score only — never the text content
                logger.warning(
                    "Content blocked by ML filter: %s=%.3f (threshold=%.2f)",
                    category,
                    score,
                    threshold,
                )
                return f"ml_{category}"

        return None


def create_ml_content_filter(
    thresholds: dict[str, float] | None = None,
) -> DetoxifyContentFilter | None:
    """Create ML content filter if detoxify is available.

    Returns None if detoxify is not installed (graceful degradation).
    """
    try:
        return DetoxifyContentFilter(thresholds=thresholds)
    except ImportError:
        logger.info(
            "Detoxify not installed — ML content filter disabled. "
            "Keyword filter remains active."
        )
        return None
