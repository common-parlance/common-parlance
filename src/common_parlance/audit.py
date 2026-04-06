"""Quick PII leakage scan and over-redaction stats.

This is a lightweight post-redaction check, not a formal audit.
The human review step is the primary defense against PII leakage.
"""

import json
import re
from dataclasses import dataclass, field

# Regex patterns to detect PII that survived redaction
_LEAK_CHECKS = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "ssn": re.compile(r"\b(?!000|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    "ip_address": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "api_key": re.compile(
        r"\b(?:sk-[a-zA-Z0-9]{20,}|sk-ant-[a-zA-Z0-9\-]{20,}"
        r"|ghp_[a-zA-Z0-9]{36,}|AKIA[0-9A-Z]{16}"
        r"|hf_[a-zA-Z0-9]{20,})\b"
    ),
    "private_key": re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----"),
    "file_path": re.compile(
        r"(?:/Users/[a-zA-Z0-9_.-]+|/home/[a-zA-Z0-9_.-]+"
        r"|C:\\Users\\[a-zA-Z0-9_.-]+)"
    ),
}

# Known placeholder patterns to count for density
_PLACEHOLDER_RE = re.compile(
    r"\[(?:NAME|EMAIL|PHONE|LOCATION|DATE|ADDRESS|GROUP|SSN|IP|SECRET|PATH"
    r"|CREDIT_CARD|URL|IBAN|MEDICAL_ID|DRIVER_LICENSE|REDACTED)"
    r"(?:_\d+)?(?::[^\]]+)?\]|<ORGANIZATION>"
)


@dataclass
class ConversationAudit:
    """Audit result for a single conversation."""

    conv_id: str
    turn_count: int
    word_count: int
    placeholder_count: int
    density: float
    preview: str
    leaks: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class AuditResult:
    """Aggregated audit results."""

    total: int
    conversations: list[ConversationAudit]
    leak_count: int = 0
    high_density_count: int = 0

    @property
    def has_leaks(self) -> bool:
        return self.leak_count > 0


def audit_conversations(rows: list, density_threshold: float = 0.25) -> AuditResult:
    """Run leakage scan and over-redaction check on staged conversation rows.

    Args:
        rows: list of (id, scrubbed_turns_json) tuples/rows
        density_threshold: flag conversations above this placeholder density

    Returns:
        AuditResult with per-conversation details and summary stats.
    """
    conversations = []

    for row in rows:
        conv_id = row[0]
        turns = json.loads(row[1])
        text = " ".join(t["content"] for t in turns)
        words = len(text.split())
        placeholders = len(_PLACEHOLDER_RE.findall(text))
        density = placeholders / max(words, 1)
        preview = turns[0]["content"][:60] if turns else ""

        # Check for PII that survived redaction
        found = {}
        for name, pattern in _LEAK_CHECKS.items():
            matches = pattern.findall(text)
            if name == "ip_address":
                matches = [
                    m
                    for m in matches
                    if not m.startswith(("0.", "1.0", "2.0", "127."))
                    and all(0 <= int(o) <= 255 for o in m.split("."))
                ]
            if matches:
                found[name] = matches[:3]

        conversations.append(
            ConversationAudit(
                conv_id=conv_id[:8],
                turn_count=len(turns),
                word_count=words,
                placeholder_count=placeholders,
                density=density,
                preview=preview,
                leaks=found,
            )
        )

    leak_count = sum(1 for c in conversations if c.leaks)
    high_density_count = sum(1 for c in conversations if c.density > density_threshold)

    return AuditResult(
        total=len(rows),
        conversations=conversations,
        leak_count=leak_count,
        high_density_count=high_density_count,
    )
