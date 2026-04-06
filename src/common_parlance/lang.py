"""Language detection with fasttext (preferred) or langdetect fallback.

Uses fasttext's lid.176.ftz model when available (installed via the
``fasttext`` extra). Falls back to langdetect for environments that
don't want C++ dependencies or network calls on first run.

    pip install common-parlance[fasttext]   # fasttext backend
    pip install common-parlance             # langdetect fallback
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Confidence threshold below which we return "unknown".
# 0.65 matches FineWeb/datatrove's LanguageFilter default.
_FASTTEXT_THRESHOLD = 0.65

_MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz"
_CACHE_DIR = Path.home() / ".cache" / "common-parlance"

_backend: str | None = None
_ft_model = None


def _get_model_path() -> Path:
    """Download lid.176.ftz to cache dir if not already present."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    model_path = _CACHE_DIR / "lid.176.ftz"
    if not model_path.exists():
        import urllib.request

        logger.info("Downloading fasttext language model (917KB)...")
        urllib.request.urlretrieve(_MODEL_URL, str(model_path))  # noqa: S310
    return model_path


def _detect_fasttext(text: str) -> str:
    """Detect language using fasttext lid.176.ftz model."""
    global _ft_model
    if _ft_model is None:
        from fasttext.FastText import _FastText

        # _FastText suppresses the useless warning from fasttext.load_model
        _ft_model = _FastText(str(_get_model_path()))

    # fasttext only reads the first line — newlines must be replaced
    cleaned = text.replace("\n", " ").strip()
    if not cleaned:
        return "unknown"

    labels, scores = _ft_model.predict(cleaned, k=1)
    lang = labels[0].replace("__label__", "")
    score = min(float(scores[0]), 1.0)

    if score < _FASTTEXT_THRESHOLD:
        return "unknown"
    return lang


def _detect_langdetect(text: str) -> str:
    """Detect language using langdetect (fallback)."""
    from langdetect import DetectorFactory, LangDetectException, detect

    DetectorFactory.seed = 0
    try:
        return detect(text)
    except LangDetectException:
        return "unknown"


def detect_language(text: str) -> str:
    """Detect the language of the given text.

    Returns an ISO 639-1 language code (e.g. ``"en"``, ``"fr"``)
    or ``"unknown"`` if detection fails or confidence is too low.
    """
    global _backend

    if _backend is None:
        try:
            import fasttext  # noqa: F401

            _backend = "fasttext"
            logger.info("Language detection: using fasttext backend")
        except ImportError:
            _backend = "langdetect"
            logger.info("Language detection: using langdetect fallback")

    if _backend == "fasttext":
        return _detect_fasttext(text)
    return _detect_langdetect(text)
