"""cp-scrub redaction benchmark — score scanners against the golden corpus.

Leaderboard MVP (see vault [[Benchmark Plan]] / [[cp-scrub Engine]]). Scores each
scanner on the SAME engine-neutral corpus the detectors are pinned to, on two axes
the research says matter (Launch&Adoption R1-F2): recall (leak rate) AND
over-redaction (false positives — what drives abandonment).

Honesty notes:
  * The corpus is authored by the cp-scrub team, so cp-scrub is the REFERENCE that
    defines ground truth (≈100% by construction) — it is not a fair competitor and
    is labelled as such. The fair results are the INDEPENDENT scanners that did not
    author the corpus (detect-secrets, Presidio, gitleaks, trufflehog, naive regex).
  * known_gap flags pin our OWN current behavior in the parity tests; for the
    benchmark every sensitive span counts as ground truth (a gap = a recall miss).
  * Each tool is scored only on the dimensions it targets (secret scanners on
    secret/*, Presidio on PII/NER), and on ALL over-redaction guards (no tool
    should flag a git SHA / UUID / package name).
  * Opaque tools (detect-secrets, gitleaks, trufflehog) and cp-scrub are scored
    at the case level — a finding anywhere in the input counts as catching that
    case's span. This is sound because each positive case carries a single
    in-scope secret span; a multi-secret case would over-credit a partial catch.

Run:  uv run python benchmark/score.py
gitleaks / trufflehog are included automatically if the binaries are on PATH.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.golden_corpus import load_corpus  # noqa: E402

# ---------------------------------------------------------------- corpus + scope

CASES = load_corpus("deterministic") + load_corpus("ner")

PII_TYPES = {"email", "phone", "ssn", "ip", "credit_card", "name", "location"}


def types_of(case) -> set[str]:
    return {s["entity_type"] for s in case.sensitive}


def is_secret(t: str) -> bool:
    return t.startswith("secret/")


def positives(case) -> bool:
    return bool(case.sensitive)


def is_guard(case) -> bool:
    return not case.sensitive and bool(case.guard)


# Which canonical types each tool is evaluated on (recall denominator).
SECRET_SCANNER = lambda t: is_secret(t)  # noqa: E731
PRESIDIO_SCOPE = lambda t: t in PII_TYPES  # noqa: E731
NAIVE_SCOPE = lambda t: is_secret(t) or t in {"email", "phone", "ssn", "ip"}  # noqa: E731
ALL_SCOPE = lambda t: True  # noqa: E731


# ---------------------------------------------------------------- adapters
# Each adapter exposes .spans(text) -> list[(start,end)] of flagged regions, OR
# .flagged(text)/.catches(text, value) for opaque tools. We normalize to two
# questions: did it flag the entity (recall) and did it flag anything (FP).


def _overlaps(spans, start, end) -> bool:
    return any(s < end and start < e for s, e in spans)


class SpanAdapter:
    """Adapter that yields character spans."""

    def __init__(self, name, scope, fn, note=""):
        self.name, self.scope, self._fn, self.note = name, scope, fn, note

    def catches(self, case, span) -> bool:
        spans = self._fn(case.input)
        return _overlaps(spans, span["start_position"], span["end_position"])

    def flagged_any(self, text) -> bool:
        return len(self._fn(text)) > 0


class BoolAdapter:
    """Adapter for opaque tools: line/string-level flag only."""

    def __init__(self, name, scope, catches_fn, flagged_fn, note=""):
        self.name, self.scope, self.note = name, scope, note
        self._catches, self._flagged = catches_fn, flagged_fn

    def catches(self, case, span) -> bool:
        return self._catches(case.input, span["entity_value"])

    def flagged_any(self, text) -> bool:
        return self._flagged(text)


# --- naive regex baseline (the "afternoon scrub" / TC-style regex-only) -------
_NAIVE = [re.compile(p) for p in [
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",  # email
    r"\b\d{3}-\d{2}-\d{4}\b",                            # ssn
    r"\b\d{3}[-.]\d{3}[-.]\d{4}\b",                      # phone
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b",                      # ipv4
    r"sk-[A-Za-z0-9]{20,}",                              # openai
    r"AKIA[0-9A-Z]{16}",                                 # aws
    r"ghp_[A-Za-z0-9]{36,}",                             # github
]]


def naive_spans(text):
    out = []
    for rx in _NAIVE:
        out += [(m.start(), m.end()) for m in rx.finditer(text)]
    return out


# --- detect-secrets (real, installed) -----------------------------------------
def _make_detect_secrets():
    from detect_secrets.core import scan
    from detect_secrets.settings import transient_settings

    plugins = ["AWSKeyDetector", "Base64HighEntropyString", "HexHighEntropyString",
               "PrivateKeyDetector", "StripeDetector", "GitHubTokenDetector",
               "JwtTokenDetector", "AzureStorageKeyDetector", "BasicAuthDetector",
               "KeywordDetector"]
    cfg = {"plugins_used": [{"name": p} for p in plugins]}

    def flagged(text):
        with transient_settings(cfg):
            for line in text.splitlines():
                if any(True for _ in scan.scan_line(line)):
                    return True
        return False

    # single-entity-per-line corpus → a line-level flag is an entity catch
    def catches(text, value):
        return flagged(text)

    return BoolAdapter("detect-secrets", SECRET_SCANNER, catches, flagged,
                       "entropy/regex plugins; line-level")


# --- Presidio vanilla (real, installed) ---------------------------------------
def _make_presidio():
    from presidio_analyzer import AnalyzerEngine
    an = AnalyzerEngine()  # default recognizers + en_core_web_lg, no allow-list

    def spans(text):
        return [(r.start, r.end) for r in an.analyze(text=text, language="en")]

    return SpanAdapter("presidio-vanilla", PRESIDIO_SCOPE, spans,
                       "default recognizers, no allow-list/thresholds")


# --- cp-scrub reference (defines ground truth) --------------------------------
def _make_cpscrub():
    from common_parlance.scrub import PresidioScrubber
    s = PresidioScrubber()

    def catches(text, value):
        return value not in s.scrub(text)

    def flagged(text):
        return s.scrub(text) != text

    return BoolAdapter("cp-scrub (reference)", ALL_SCOPE, catches, flagged,
                       "authored the corpus → ≈100% by construction")


# --- gitleaks / trufflehog CLI (auto-included if on PATH) ---------------------
# Opaque, single-secret-per-case → line-level flag (a finding on the input == a
# catch). gitleaks reports the secret with vendor prefixes stripped, so a
# substring match against the corpus entity_value is unreliable; count findings.


def _make_gitleaks():
    exe = shutil.which("gitleaks")
    if not exe:
        return None

    def n_findings(text):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "blob.txt"
            src.write_text(text)
            rep = Path(d) / "report.json"
            p = subprocess.run(
                [exe, "detect", "--no-git", "--source", str(src),
                 "--report-format", "json", "--report-path", str(rep)],
                capture_output=True, text=True)
            # gitleaks: 0 = clean, 1 = leaks found, >1 = real error. Fail loud
            # on errors rather than silently reporting 0% recall for the tool.
            if p.returncode not in (0, 1):
                raise RuntimeError(f"gitleaks exit {p.returncode}: {p.stderr[:200]}")
            try:
                return len(json.loads(rep.read_text() or "[]"))
            except (json.JSONDecodeError, FileNotFoundError):
                return 0

    def flagged(text):
        return n_findings(text) > 0

    return BoolAdapter("gitleaks", SECRET_SCANNER,
                       lambda text, value: flagged(text), flagged, "CLI, --no-git")


def _make_trufflehog():
    exe = shutil.which("trufflehog")
    if not exe:
        return None

    def n_findings(text):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "blob.txt").write_text(text)
            p = subprocess.run(
                [exe, "filesystem", d, "--no-verification", "--json"],
                capture_output=True, text=True)
        n = 0
        for line in p.stdout.splitlines():
            try:
                if json.loads(line).get("DetectorName"):
                    n += 1
            except json.JSONDecodeError:
                pass
        return n

    def flagged(text):
        return n_findings(text) > 0

    return BoolAdapter("trufflehog", SECRET_SCANNER,
                       lambda text, value: flagged(text), flagged,
                       "CLI, --no-verification (verification-oriented)")


# ---------------------------------------------------------------- scoring

def build_adapters():
    ads = [SpanAdapter("naive-regex", NAIVE_SCOPE, naive_spans,
                       "email/phone/ssn/ip + 3 key prefixes")]
    ads.append(_make_detect_secrets())
    ads.append(_make_presidio())
    for mk in (_make_gitleaks, _make_trufflehog):
        a = mk()
        if a:
            ads.append(a)
    ads.append(_make_cpscrub())  # reference last
    return ads


def score(adapter):
    recall_hit = recall_n = 0
    for c in CASES:
        in_scope = [s for s in c.sensitive if adapter.scope(s["entity_type"])]
        for span in in_scope:
            recall_n += 1
            if adapter.catches(c, span):
                recall_hit += 1
    fp = guard_n = 0
    for c in CASES:
        if is_guard(c):
            guard_n += 1
            if adapter.flagged_any(c.input):
                fp += 1
    return recall_hit, recall_n, fp, guard_n


def main():
    print(f"# cp-scrub redaction benchmark — {len(CASES)} corpus cases\n")
    gl = "yes" if shutil.which("gitleaks") else "NOT on PATH"
    th = "yes" if shutil.which("trufflehog") else "NOT on PATH"
    print(f"_gitleaks: {gl} · trufflehog: {th}_\n")
    print("| scanner | recall (caught/total) | over-redaction (FP/guards) | notes |")
    print("|---|---|---|---|")
    for a in build_adapters():
        rh, rn, fp, gn = score(a)
        rec = f"{rh}/{rn} ({100 * rh // rn if rn else 0}%)" if rn else "n/a"
        ovr = f"{fp}/{gn} ({100 * fp // gn if gn else 0}%)" if gn else "n/a"
        print(f"| {a.name} | {rec} | {ovr} | {a.note} |")
    print("\n_Recall = sensitive spans in the tool's scope that it flagged. "
          "Over-redaction = must-survive guard strings it wrongly flagged. "
          "cp-scrub authored the corpus (reference, not a fair entrant)._")


if __name__ == "__main__":
    main()
