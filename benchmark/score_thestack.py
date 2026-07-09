"""Scale over-redaction probe — run scrubbers over real code.

Answers the #1 adoption worry: "will this shred my code?" Real source is mostly
NOT secret, so a high flag-rate ≈ over-redaction at scale. Compares cp-scrub
against entropy-driven detect-secrets and a naive baseline.

Uses `bigcode/the-stack-smol-xs` (ungated, 100 files/language) via direct file
download — small and reliable (the full the-stack-dedup streams ~3TB and is
gated). Emits ONLY aggregate rates, never sample text (no-redistribute hygiene).

Usage:  uv run python benchmark/score_thestack.py [lang1,lang2,...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark.score import _make_detect_secrets, naive_spans  # noqa: E402
from common_parlance.scrub import RegexScrubber, normalize_text  # noqa: E402

DEFAULT_LANGS = ["python", "javascript", "java", "go", "c", "ruby", "php", "rust"]
LANGS = sys.argv[1].split(",") if len(sys.argv) > 1 else DEFAULT_LANGS


def load_code():
    from huggingface_hub import hf_hub_download

    texts = []
    for lang in LANGS:
        try:
            p = hf_hub_download(
                "bigcode/the-stack-smol-xs", f"data/{lang}/data.json",
                repo_type="dataset",
            )
        except Exception as e:
            print(f"  (skip {lang}: {type(e).__name__})")
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                c = json.loads(line).get("content", "")
                if c.strip():
                    texts.append(c)
    return texts


def main():
    texts = load_code()
    reg = RegexScrubber()
    detect = _make_detect_secrets()
    # cp-scrub is handled separately (one scrub per file, reused for both its
    # total-flag and [SECRET]-only metrics).
    others = {
        "naive-regex": lambda t: len(naive_spans(t)) > 0,
        "detect-secrets": detect.flagged_any,
    }
    counts = dict.fromkeys([*others, "cp-scrub (regex)"], 0)
    cp_secret = 0  # files where cp-scrub emits a [SECRET] (over-redaction-on-secret)
    for t in texts:
        for k, fn in others.items():
            try:
                if fn(t):
                    counts[k] += 1
            except Exception:
                pass
        scrubbed = reg.scrub(t)  # scrub once
        # Compare to the NFKC-normalized input, not the raw text — scrub()
        # normalizes first, so `!= t` would count benign fullwidth/zero-width
        # folding as "redaction" and inflate the rate.
        if scrubbed != normalize_text(t):
            counts["cp-scrub (regex)"] += 1
        # only count a [SECRET] that cp-scrub *introduced* — not source that
        # already contained the literal "[SECRET]" (e.g. a log template).
        if "[SECRET]" in scrubbed and "[SECRET]" not in t:
            cp_secret += 1

    n = len(texts)
    print(f"\n# the-stack-smol-xs over-redaction probe — {n} real code files "
          f"({', '.join(LANGS)})\n")
    print("| tool | files flagged | flag rate |")
    print("|---|---|---|")
    for k in counts:
        rate = 100 * counts[k] // n if n else 0
        print(f"| {k} | {counts[k]}/{n} | {rate}% |")
    cps = 100 * cp_secret // n if n else 0
    print(f"| cp-scrub ([SECRET] only) | {cp_secret}/{n} | {cps}% |")
    print("\n_detect-secrets is secret-only, so its rate ≈ false positives. "
          "cp-scrub's total includes legitimate URL/path/email reduction; its "
          "[SECRET]-only rate is the fair secret-FP comparison. (Not pure FP: real "
          "source does contain some secrets.)_")


if __name__ == "__main__":
    main()
