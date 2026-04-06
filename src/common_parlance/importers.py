"""Import conversations from external file formats.

Auto-detects format from file structure. Supported layouts:
  - Messages JSONL: {"messages": [{"role", "content"}]} per line
  - ShareGPT JSONL/JSON: {"conversations": [{"from", "value"}]}
  - Export ZIP with mapping tree (conversations.json with "mapping" nodes)
  - Export ZIP with chat_messages (conversations.json with "chat_messages")
  - SQLite with chat table (JSON blob per row, message tree)
  - Thread directories (messages.jsonl with nested content blocks)

Imported conversations enter the exchanges table as synthetic exchanges,
flowing through the same process → review → upload pipeline as proxy captures.
"""

import hashlib
import json
import logging
import sqlite3
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Tracks import statistics."""

    imported: int = 0
    skipped_empty: int = 0
    skipped_duplicate: int = 0
    skipped_malformed: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "ImportResult") -> None:
        """Merge another result into this one."""
        self.imported += other.imported
        self.skipped_empty += other.skipped_empty
        self.skipped_duplicate += other.skipped_duplicate
        self.skipped_malformed += other.skipped_malformed
        self.errors.extend(other.errors)


def content_hash(messages: list[dict]) -> str:
    """SHA-256 hash of normalized conversation content for dedup."""
    normalized = json.dumps(
        [{"role": m["role"], "content": m["content"]} for m in messages],
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(normalized.encode()).hexdigest()


def to_synthetic_exchange(messages: list[dict]) -> tuple[str, str] | None:
    """Convert role/content messages to synthetic request_json/response_json.

    Returns (request_json, response_json) compatible with extract_turns(),
    or None if the conversation is unusable (too short, no assistant turn).
    """
    turns = [
        m
        for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]

    if len(turns) < 2:
        return None

    # Drop trailing user message if conversation doesn't end on assistant
    if turns[-1]["role"] != "assistant":
        turns = turns[:-1]

    if len(turns) < 2 or turns[-1]["role"] != "assistant":
        return None

    # Split: all except last assistant → request, last assistant → response
    request_messages = turns[:-1]
    last_assistant = turns[-1]

    request_json = json.dumps({"messages": request_messages})
    response_json = json.dumps(
        {
            "choices": [
                {"message": {"role": "assistant", "content": last_assistant["content"]}}
            ]
        }
    )
    return request_json, response_json


# --- Format detection ---

# Internal format identifiers (deliberately generic, no provider names)
FMT_MESSAGES_JSONL = "messages-jsonl"
FMT_SHAREGPT = "sharegpt"
FMT_ZIP_MAPPING_TREE = "zip-mapping-tree"
FMT_ZIP_CHAT_MESSAGES = "zip-chat-messages"
FMT_SQLITE_CHAT = "sqlite-chat"
FMT_THREAD_DIR = "thread-dir"


def detect_format(path: Path) -> str | None:
    """Auto-detect file format from content. Returns an internal format ID or None."""
    if path.is_dir():
        return _detect_directory_format(path)

    if path.suffix == ".zip":
        return _detect_zip_format(path)

    if path.suffix in (".db", ".sqlite", ".sqlite3"):
        return _detect_sqlite_format(path)

    if path.suffix not in (".jsonl", ".json"):
        return None

    return _detect_json_format(path)


def _detect_directory_format(path: Path) -> str | None:
    """Detect format of a directory (thread dirs with messages.jsonl)."""
    # Thread directory: contains messages.jsonl directly
    if (path / "messages.jsonl").exists():
        return FMT_THREAD_DIR

    # Parent of thread directories
    for child in path.iterdir():
        if child.is_dir() and (child / "messages.jsonl").exists():
            return FMT_THREAD_DIR

    return None


def _detect_zip_format(path: Path) -> str | None:
    """Detect format of a ZIP file by inspecting contents."""
    try:
        with zipfile.ZipFile(path) as zf:
            if "conversations.json" not in zf.namelist():
                return None
            # Peek at the JSON to distinguish tree vs flat
            try:
                raw = zf.read("conversations.json").decode("utf-8")
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None

            if not isinstance(data, list) or not data:
                return None

            first = data[0]
            if not isinstance(first, dict):
                return None

            if "mapping" in first:
                return FMT_ZIP_MAPPING_TREE
            if "chat_messages" in first:
                return FMT_ZIP_CHAT_MESSAGES

            return None
    except zipfile.BadZipFile:
        return None


def _detect_sqlite_format(path: Path) -> str | None:
    """Detect format of a SQLite database."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chat'"
            )
            if cursor.fetchone():
                return FMT_SQLITE_CHAT
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return None


def _detect_json_format(path: Path) -> str | None:
    """Detect format of a JSON/JSONL file.

    Only reads the first line for detection to avoid loading large files.
    """
    first_line = None
    for encoding in ("utf-8", "latin-1"):
        try:
            with open(path, encoding=encoding) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        first_line = line
                        break
            break
        except UnicodeDecodeError:
            continue
        except Exception:
            return None

    if not first_line:
        return None
    try:
        obj = json.loads(first_line)
    except json.JSONDecodeError:
        return None

    if isinstance(obj, dict):
        if "messages" in obj:
            return FMT_MESSAGES_JSONL
        if "conversations" in obj or "conversation" in obj:
            return FMT_SHAREGPT
        # Thread-style message line (nested content blocks)
        if "role" in obj and "content" in obj and isinstance(obj.get("content"), list):
            return FMT_THREAD_DIR

    # JSON array: check first element
    if isinstance(obj, list) and obj:
        first = obj[0]
        if isinstance(first, dict):
            if "messages" in first:
                return FMT_MESSAGES_JSONL
            if "conversations" in first or "conversation" in first:
                return FMT_SHAREGPT

    return None


# --- Parsers ---
# Each parser returns a list of conversations, where each conversation
# is a list of {"role": ..., "content": ...} dicts.


def parse_messages_jsonl(path: Path) -> tuple[list[list[dict]], list[str]]:
    """Parse messages JSONL. Each line: {"messages": [{"role", "content"}]}.

    Streams line-by-line to handle large files without loading into memory.
    """
    conversations = []
    errors = []

    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"Line {i}: invalid JSON")
                continue

            messages = obj.get("messages")
            if not isinstance(messages, list):
                errors.append(f"Line {i}: missing 'messages' array")
                continue

            turns = []
            for msg in messages:
                role = msg.get("role")
                content = msg.get("content")
                if role and content and isinstance(content, str):
                    turns.append({"role": role, "content": content})

            if turns:
                conversations.append(turns)

    return conversations, errors


def parse_sharegpt(path: Path) -> tuple[list[list[dict]], list[str]]:
    """Parse ShareGPT format. Maps human→user, gpt→assistant."""
    conversations = []
    errors = []

    role_map = {"human": "user", "gpt": "assistant", "system": "system"}

    text = path.read_text(encoding="utf-8")
    text = text.strip()

    # Could be JSONL (one object per line) or a JSON array
    objects = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            objects = parsed
        elif isinstance(parsed, dict):
            objects = [parsed]
    except json.JSONDecodeError:
        # Try as JSONL
        for i, line in enumerate(text.split("\n"), 1):
            line = line.strip()
            if not line:
                continue
            try:
                objects.append(json.loads(line))
            except json.JSONDecodeError:
                errors.append(f"Line {i}: invalid JSON")

    for obj in objects:
        convs = obj.get("conversations") or obj.get("conversation")
        if not isinstance(convs, list):
            continue

        turns = []
        for entry in convs:
            from_role = entry.get("from", "")
            value = entry.get("value", "")
            mapped = role_map.get(from_role)
            if mapped and value and isinstance(value, str):
                turns.append({"role": mapped, "content": value})

        if turns:
            conversations.append(turns)

    return conversations, errors


def parse_zip_mapping_tree(path: Path) -> tuple[list[list[dict]], list[str]]:
    """Parse export ZIP with mapping-tree conversations."""
    conversations = []
    errors = []

    try:
        with zipfile.ZipFile(path) as zf:
            raw = zf.read("conversations.json").decode("utf-8")
    except (zipfile.BadZipFile, KeyError) as e:
        return [], [f"Failed to read ZIP: {e}"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [], ["conversations.json contains invalid JSON"]

    if not isinstance(data, list):
        return [], ["conversations.json is not an array"]

    for conv in data:
        mapping = conv.get("mapping")
        if not isinstance(mapping, dict):
            continue

        turns = _flatten_mapping_tree(mapping)
        if turns:
            conversations.append(turns)

    return conversations, errors


def _flatten_mapping_tree(mapping: dict) -> list[dict]:
    """Walk a mapping tree to extract a linear conversation.

    Follows the first child at each branch point.
    """
    # Build children lookup
    children: dict[str | None, list[str]] = {}
    for node_id, node in mapping.items():
        parent = node.get("parent")
        children.setdefault(parent, []).append(node_id)

    # Find root (parent is None)
    roots = children.get(None, [])
    if not roots:
        return []

    # DFS from root, following first child
    turns = []
    current = roots[0]
    visited = set()
    while current and current not in visited:
        visited.add(current)
        node = mapping.get(current, {})
        msg = node.get("message")
        if msg and isinstance(msg, dict):
            author = msg.get("author", {})
            role = author.get("role") if isinstance(author, dict) else None
            content_obj = msg.get("content", {})

            # Extract text from content.parts
            text = ""
            if isinstance(content_obj, dict):
                parts = content_obj.get("parts", [])
                if isinstance(parts, list):
                    text_parts = [p for p in parts if isinstance(p, str)]
                    text = "\n".join(text_parts)

            if role in ("user", "assistant") and text.strip():
                turns.append({"role": role, "content": text.strip()})

        # Follow first child
        node_children = children.get(current, [])
        current = node_children[0] if node_children else None

    return turns


def parse_zip_chat_messages(path: Path) -> tuple[list[list[dict]], list[str]]:
    """Parse export ZIP with flat chat_messages conversations."""
    conversations = []
    errors = []

    try:
        with zipfile.ZipFile(path) as zf:
            raw = zf.read("conversations.json").decode("utf-8")
    except (zipfile.BadZipFile, KeyError) as e:
        return [], [f"Failed to read ZIP: {e}"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return [], ["conversations.json contains invalid JSON"]

    if not isinstance(data, list):
        return [], ["conversations.json is not an array"]

    # Role mapping: some exports use "human" instead of "user"
    role_map = {"human": "user", "user": "user", "assistant": "assistant"}

    for conv in data:
        chat_messages = conv.get("chat_messages")
        if not isinstance(chat_messages, list):
            continue

        turns = []
        for msg in chat_messages:
            sender = msg.get("sender", "")
            text = msg.get("text", "")
            role = role_map.get(sender)
            if role and text and isinstance(text, str) and text.strip():
                turns.append({"role": role, "content": text.strip()})

        if turns:
            conversations.append(turns)

    return conversations, errors


def parse_sqlite_chat(path: Path) -> tuple[list[list[dict]], list[str]]:
    """Parse SQLite database with a chat table containing JSON conversation blobs."""
    conversations = []
    errors = []

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return [], [f"Failed to open database: {e}"]

    try:
        cursor = conn.execute("SELECT id, chat FROM chat")
        for row_id, chat_json in cursor:
            if not chat_json:
                continue
            try:
                chat_data = json.loads(chat_json)
            except (json.JSONDecodeError, TypeError):
                errors.append(f"Row {row_id}: invalid JSON in chat column")
                continue

            if not isinstance(chat_data, dict):
                continue

            turns = _extract_sqlite_chat_turns(chat_data)
            if turns:
                conversations.append(turns)
    except sqlite3.Error as e:
        errors.append(f"Database query failed: {e}")
    finally:
        conn.close()

    return conversations, errors


def _extract_sqlite_chat_turns(chat_data: dict) -> list[dict]:
    """Extract turns from a SQLite chat JSON blob.

    Handles both flat message lists and tree structures with parentId/childrenIds.
    """
    messages = chat_data.get("messages")
    if not isinstance(messages, list):
        return []

    # Check if messages have tree structure (parentId/childrenIds)
    has_tree = any(
        isinstance(m, dict) and ("parentId" in m or "childrenIds" in m)
        for m in messages
    )

    if has_tree:
        return _flatten_sqlite_message_tree(messages, chat_data)

    # Flat list
    turns = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content and isinstance(content, str):
            turns.append({"role": role, "content": content.strip()})

    return turns


def _flatten_sqlite_message_tree(messages: list[dict], chat_data: dict) -> list[dict]:
    """Flatten a message tree using history.currentId to find the active branch."""
    # Build lookup by message ID
    by_id: dict[str, dict] = {}
    for msg in messages:
        if isinstance(msg, dict) and "id" in msg:
            by_id[msg["id"]] = msg

    # Also check history.messages which may have more complete data
    history = chat_data.get("history", {})
    if isinstance(history, dict):
        hist_messages = history.get("messages", {})
        if isinstance(hist_messages, dict):
            for msg_id, msg in hist_messages.items():
                if isinstance(msg, dict):
                    by_id.setdefault(msg_id, msg)

    # Find the active leaf via history.currentId
    current_id = None
    if isinstance(history, dict):
        current_id = history.get("currentId")

    # Walk backwards from current_id to build the path
    if current_id and current_id in by_id:
        path = []
        visited = set()
        node_id = current_id
        while node_id and node_id in by_id and node_id not in visited:
            visited.add(node_id)
            path.append(by_id[node_id])
            node_id = by_id[node_id].get("parentId")
        path.reverse()
    else:
        # Fallback: use flat message list order
        path = messages

    turns = []
    for msg in path:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content and isinstance(content, str):
            turns.append({"role": role, "content": content.strip()})

    return turns


def parse_thread_dir(path: Path) -> tuple[list[list[dict]], list[str]]:
    """Parse thread directory or parent of thread directories.

    Handles directories containing messages.jsonl (single thread)
    or subdirectories each containing messages.jsonl (multiple threads).
    Also handles a standalone messages.jsonl file.
    """
    conversations = []
    errors = []

    if path.is_file():
        # Standalone JSONL file with thread-style messages
        turns, errs = _parse_thread_messages_file(path)
        errors.extend(errs)
        if turns:
            conversations.append(turns)
        return conversations, errors

    # Single thread directory
    messages_file = path / "messages.jsonl"
    if messages_file.exists():
        turns, errs = _parse_thread_messages_file(messages_file)
        errors.extend(errs)
        if turns:
            conversations.append(turns)
        return conversations, errors

    # Parent of thread directories
    for child in sorted(path.iterdir()):
        if child.is_dir():
            child_messages = child / "messages.jsonl"
            if child_messages.exists():
                turns, errs = _parse_thread_messages_file(child_messages)
                errors.extend(errs)
                if turns:
                    conversations.append(turns)

    return conversations, errors


def _parse_thread_messages_file(path: Path) -> tuple[list[dict], list[str]]:
    """Parse a single messages.jsonl thread file.

    Each line is a message object. Content may be:
      - A plain string in "content" field
      - Nested in content[].text.value (Assistants API style)

    Streams line-by-line to handle large files without loading into memory.
    """
    turns = []
    errors = []

    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"{path.name} line {i}: invalid JSON")
                continue

            if not isinstance(msg, dict):
                continue

            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue

            content = _extract_thread_content(msg.get("content"))
            if content:
                turns.append({"role": role, "content": content})

    return turns, errors


def _extract_thread_content(content) -> str:
    """Extract text from thread message content field.

    Handles both plain strings and nested content block arrays:
      [{"type": "text", "text": {"value": "..."}}]
    """
    if isinstance(content, str):
        return content.strip()

    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_obj = block.get("text", {})
                if isinstance(text_obj, dict):
                    value = text_obj.get("value", "")
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip())
                elif isinstance(text_obj, str) and text_obj.strip():
                    parts.append(text_obj.strip())
        return "\n".join(parts)

    return ""


# --- Main import orchestrator ---


_PARSERS = {
    FMT_MESSAGES_JSONL: parse_messages_jsonl,
    FMT_SHAREGPT: parse_sharegpt,
    FMT_ZIP_MAPPING_TREE: parse_zip_mapping_tree,
    FMT_ZIP_CHAT_MESSAGES: parse_zip_chat_messages,
    FMT_SQLITE_CHAT: parse_sqlite_chat,
    FMT_THREAD_DIR: parse_thread_dir,
}


def import_conversations(
    store,
    path: Path,
    format: str | None = None,
    dry_run: bool = False,
) -> ImportResult:
    """Import conversations from a file or directory into the exchanges table.

    For directories without a recognized format, recurses into children
    and tries each file individually. Deduplicates by content hash.
    """
    result = ImportResult()

    fmt = format or detect_format(path)

    # Directory with no recognized format: recurse into children
    if fmt is None and path.is_dir():
        for child in sorted(path.iterdir()):
            child_result = import_conversations(
                store=store, path=child, dry_run=dry_run
            )
            result.merge(child_result)
        return result

    if fmt is None:
        result.errors.append(f"Could not detect format for {path.name}")
        return result

    parser = _PARSERS.get(fmt)
    if parser is None:
        result.errors.append(f"Unknown format: {fmt}")
        return result

    conversations, errors = parser(path)
    result.errors.extend(errors)
    result.skipped_malformed = len(errors)

    session_id = f"import-{uuid.uuid4()}"

    for conv in conversations:
        exchange = to_synthetic_exchange(conv)
        if exchange is None:
            result.skipped_empty += 1
            continue

        request_json, response_json = exchange
        hash_val = content_hash(conv)

        if dry_run:
            result.imported += 1
            continue

        eid = store.log_exchange_with_hash(
            session_id, request_json, response_json, hash_val
        )
        if eid is None:
            result.skipped_duplicate += 1
        else:
            result.imported += 1

    return result
