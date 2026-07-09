"""Language detection with fasttext (preferred) or py3langid fallback.

Uses fasttext's lid.176.ftz model when available (installed via the
``fasttext`` extra). Falls back to py3langid — a maintained, pure-Python
port of langid.py — for environments that don't want C++ dependencies or
network calls on first run.

    pip install common-parlance[fasttext]   # fasttext backend
    pip install common-parlance             # py3langid fallback
"""

import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Confidence threshold below which we return "unknown".
# 0.65 matches FineWeb/datatrove's LanguageFilter default.
_FASTTEXT_THRESHOLD = 0.65

_MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz"
# SHA-256 of the upstream lid.176.ftz (pinned 2026-06-22; matches the
# widely-published value). The model is fetched over the network and parsed by
# the fasttext C++ loader, so verify integrity before trusting it. Update this
# alongside _MODEL_URL if the model is ever intentionally changed.
_MODEL_SHA256 = "8f3472cfe8738a7b6099e8e999c3cbfae0dcd15696aac7d7738a8039db603e83"
_CACHE_DIR = Path.home() / ".cache" / "common-parlance"

_backend: str | None = None
_ft_model = None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_model_path() -> Path:
    """Return the cached lid.176.ftz, downloading it if needed.

    The file is integrity-checked against a pinned SHA-256 whether it was just
    downloaded or already cached, so a tampered CDN response or a poisoned cache
    is rejected rather than loaded by the fasttext parser.
    """
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    model_path = _CACHE_DIR / "lid.176.ftz"

    if not model_path.exists():
        import urllib.request

        logger.info("Downloading fasttext language model (~917KB)...")
        tmp_path = model_path.with_name("lid.176.ftz.tmp")
        urllib.request.urlretrieve(_MODEL_URL, str(tmp_path))  # noqa: S310
        digest = _sha256(tmp_path)
        if digest != _MODEL_SHA256:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"fasttext model checksum mismatch (expected {_MODEL_SHA256}, "
                f"got {digest}); refusing to use a possibly-tampered model."
            )
        tmp_path.replace(model_path)
    else:
        digest = _sha256(model_path)
        if digest != _MODEL_SHA256:
            model_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"cached fasttext model at {model_path} failed its checksum "
                f"(expected {_MODEL_SHA256}, got {digest}); removed it — re-run "
                "to re-download."
            )

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


def _detect_langid(text: str) -> str:
    """Detect language using py3langid (fallback)."""
    import py3langid as langid

    # py3langid uses a fixed naive-Bayes model with no sampling, so it is
    # deterministic and needs no seed. It always returns a label, even for
    # empty/garbage input, so guard empties to "unknown" — matching the
    # fasttext path and keeping non-English warnings conservative (an empty
    # or undetectable conversation is never silently tagged "en").
    cleaned = text.strip()
    if not cleaned:
        return "unknown"

    try:
        lang, _score = langid.classify(cleaned)
        return lang
    except Exception:
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
            _backend = "langid"
            logger.info("Language detection: using py3langid fallback")

    if _backend == "fasttext":
        return _detect_fasttext(text)
    return _detect_langid(text)
