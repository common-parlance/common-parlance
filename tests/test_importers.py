"""Tests for conversation importers."""

import json
import sqlite3
import zipfile
from unittest.mock import MagicMock

from common_parlance.importers import (
    FMT_MESSAGES_JSONL,
    FMT_SHAREGPT,
    FMT_SQLITE_CHAT,
    FMT_THREAD_DIR,
    FMT_ZIP_CHAT_MESSAGES,
    FMT_ZIP_MAPPING_TREE,
    ImportResult,
    _extract_thread_content,
    _flatten_mapping_tree,
    content_hash,
    detect_format,
    import_conversations,
    parse_messages_jsonl,
    parse_sharegpt,
    parse_sqlite_chat,
    parse_thread_dir,
    parse_zip_chat_messages,
    parse_zip_mapping_tree,
    to_synthetic_exchange,
)

# --- content_hash ---


class TestContentHash:
    def test_deterministic(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        assert content_hash(msgs) == content_hash(msgs)

    def test_different_content(self):
        a = [{"role": "user", "content": "hi"}]
        b = [{"role": "user", "content": "bye"}]
        assert content_hash(a) != content_hash(b)

    def test_ignores_extra_fields(self):
        a = [{"role": "user", "content": "hi", "name": "Alice"}]
        b = [{"role": "user", "content": "hi"}]
        assert content_hash(a) == content_hash(b)


# --- to_synthetic_exchange ---


class TestToSyntheticExchange:
    def test_basic(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = to_synthetic_exchange(msgs)
        assert result is not None
        req, resp = result
        assert json.loads(req)["messages"] == [{"role": "user", "content": "hi"}]
        choices = json.loads(resp)["choices"]
        assert choices[0]["message"]["content"] == "hello"

    def test_multi_turn(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {"role": "assistant", "content": "d"},
        ]
        result = to_synthetic_exchange(msgs)
        assert result is not None
        req, resp = result
        req_msgs = json.loads(req)["messages"]
        assert len(req_msgs) == 3  # all except last assistant
        assert json.loads(resp)["choices"][0]["message"]["content"] == "d"

    def test_drops_trailing_user(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        result = to_synthetic_exchange(msgs)
        assert result is not None
        resp = json.loads(result[1])
        assert resp["choices"][0]["message"]["content"] == "b"

    def test_too_short(self):
        assert to_synthetic_exchange([{"role": "user", "content": "hi"}]) is None

    def test_no_assistant(self):
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
        ]
        assert to_synthetic_exchange(msgs) is None

    def test_filters_empty_content(self):
        msgs = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "hi"},
        ]
        assert to_synthetic_exchange(msgs) is None

    def test_filters_system(self):
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        result = to_synthetic_exchange(msgs)
        assert result is not None
        req_msgs = json.loads(result[0])["messages"]
        assert all(m["role"] != "system" for m in req_msgs)


# --- detect_format ---


class TestDetectFormat:
    def test_messages_jsonl(self, tmp_path):
        f = tmp_path / "data.jsonl"
        f.write_text(json.dumps({"messages": [{"role": "user", "content": "hi"}]}))
        assert detect_format(f) == FMT_MESSAGES_JSONL

    def test_sharegpt_jsonl(self, tmp_path):
        f = tmp_path / "data.jsonl"
        f.write_text(json.dumps({"conversations": [{"from": "human", "value": "hi"}]}))
        assert detect_format(f) == FMT_SHAREGPT

    def test_sharegpt_conversation_key(self, tmp_path):
        f = tmp_path / "data.jsonl"
        f.write_text(json.dumps({"conversation": [{"from": "human", "value": "hi"}]}))
        assert detect_format(f) == FMT_SHAREGPT

    def test_zip_mapping_tree(self, tmp_path):
        f = tmp_path / "export.zip"
        data = [{"mapping": {"root": {"parent": None}}}]
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("conversations.json", json.dumps(data))
        assert detect_format(f) == FMT_ZIP_MAPPING_TREE

    def test_zip_chat_messages(self, tmp_path):
        f = tmp_path / "export.zip"
        data = [{"chat_messages": [{"sender": "human", "text": "hi"}]}]
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("conversations.json", json.dumps(data))
        assert detect_format(f) == FMT_ZIP_CHAT_MESSAGES

    def test_zip_without_conversations(self, tmp_path):
        f = tmp_path / "random.zip"
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("other.txt", "hello")
        assert detect_format(f) is None

    def test_sqlite_chat(self, tmp_path):
        f = tmp_path / "webui.db"
        conn = sqlite3.connect(str(f))
        conn.execute("CREATE TABLE chat (id TEXT, chat TEXT)")
        conn.close()
        assert detect_format(f) == FMT_SQLITE_CHAT

    def test_sqlite_no_chat_table(self, tmp_path):
        f = tmp_path / "other.db"
        conn = sqlite3.connect(str(f))
        conn.execute("CREATE TABLE users (id TEXT)")
        conn.close()
        assert detect_format(f) is None

    def test_thread_dir_single(self, tmp_path):
        thread = tmp_path / "thread_1"
        thread.mkdir()
        (thread / "messages.jsonl").write_text("")
        assert detect_format(tmp_path) == FMT_THREAD_DIR

    def test_thread_dir_direct(self, tmp_path):
        (tmp_path / "messages.jsonl").write_text("")
        assert detect_format(tmp_path) == FMT_THREAD_DIR

    def test_unknown_extension(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c")
        assert detect_format(f) is None

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        assert detect_format(f) is None

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.jsonl"
        f.write_text("{not valid json")
        assert detect_format(f) is None

    def test_json_array_messages(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text(json.dumps([{"messages": [{"role": "user", "content": "hi"}]}]))
        assert detect_format(f) == FMT_MESSAGES_JSONL

    def test_json_array_sharegpt(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text(
            json.dumps([{"conversations": [{"from": "human", "value": "hi"}]}])
        )
        assert detect_format(f) == FMT_SHAREGPT

    def test_thread_jsonl_nested_content(self, tmp_path):
        f = tmp_path / "messages.jsonl"
        msg = {"role": "user", "content": [{"type": "text", "text": {"value": "hi"}}]}
        f.write_text(json.dumps(msg))
        assert detect_format(f) == FMT_THREAD_DIR


# --- parse_messages_jsonl ---


class TestParseMessagesJsonl:
    def test_basic(self, tmp_path):
        f = tmp_path / "data.jsonl"
        line = json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ]
            }
        )
        f.write_text(line)
        convs, errors = parse_messages_jsonl(f)
        assert len(convs) == 1
        assert len(convs[0]) == 2
        assert errors == []

    def test_multi_line(self, tmp_path):
        f = tmp_path / "data.jsonl"
        lines = []
        for i in range(3):
            lines.append(
                json.dumps(
                    {
                        "messages": [
                            {"role": "user", "content": f"q{i}"},
                            {"role": "assistant", "content": f"a{i}"},
                        ]
                    }
                )
            )
        f.write_text("\n".join(lines))
        convs, errors = parse_messages_jsonl(f)
        assert len(convs) == 3
        assert errors == []

    def test_skips_bad_json(self, tmp_path):
        f = tmp_path / "data.jsonl"
        f.write_text('{"messages": [{"role": "user", "content": "hi"}]}\n{bad json\n')
        convs, errors = parse_messages_jsonl(f)
        assert len(convs) == 1
        assert len(errors) == 1

    def test_skips_missing_messages(self, tmp_path):
        f = tmp_path / "data.jsonl"
        f.write_text('{"other": "data"}\n')
        convs, errors = parse_messages_jsonl(f)
        assert len(convs) == 0
        assert len(errors) == 1

    def test_skips_non_string_content(self, tmp_path):
        f = tmp_path / "data.jsonl"
        line = json.dumps(
            {
                "messages": [
                    {"role": "user", "content": ["array", "content"]},
                    {"role": "assistant", "content": "hello"},
                ]
            }
        )
        f.write_text(line)
        convs, errors = parse_messages_jsonl(f)
        assert len(convs) == 1
        assert len(convs[0]) == 1  # only the assistant turn


# --- parse_sharegpt ---


class TestParseSharegpt:
    def test_basic(self, tmp_path):
        f = tmp_path / "data.jsonl"
        line = json.dumps(
            {
                "conversations": [
                    {"from": "human", "value": "hi"},
                    {"from": "gpt", "value": "hello"},
                ]
            }
        )
        f.write_text(line)
        convs, errors = parse_sharegpt(f)
        assert len(convs) == 1
        assert convs[0][0] == {"role": "user", "content": "hi"}
        assert convs[0][1] == {"role": "assistant", "content": "hello"}

    def test_json_array(self, tmp_path):
        f = tmp_path / "data.json"
        data = [
            {
                "conversations": [
                    {"from": "human", "value": "a"},
                    {"from": "gpt", "value": "b"},
                ]
            },
            {
                "conversations": [
                    {"from": "human", "value": "c"},
                    {"from": "gpt", "value": "d"},
                ]
            },
        ]
        f.write_text(json.dumps(data))
        convs, errors = parse_sharegpt(f)
        assert len(convs) == 2

    def test_system_role(self, tmp_path):
        f = tmp_path / "data.jsonl"
        line = json.dumps(
            {
                "conversations": [
                    {"from": "system", "value": "You are helpful"},
                    {"from": "human", "value": "hi"},
                    {"from": "gpt", "value": "hello"},
                ]
            }
        )
        f.write_text(line)
        convs, errors = parse_sharegpt(f)
        assert len(convs) == 1
        assert convs[0][0]["role"] == "system"

    def test_conversation_key(self, tmp_path):
        """Accepts 'conversation' (singular) as well."""
        f = tmp_path / "data.jsonl"
        line = json.dumps(
            {
                "conversation": [
                    {"from": "human", "value": "hi"},
                    {"from": "gpt", "value": "hello"},
                ]
            }
        )
        f.write_text(line)
        convs, errors = parse_sharegpt(f)
        assert len(convs) == 1


# --- parse_zip_mapping_tree ---


class TestParseZipMappingTree:
    def _make_zip(self, tmp_path, conversations_data):
        f = tmp_path / "export.zip"
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("conversations.json", json.dumps(conversations_data))
        return f

    def test_basic(self, tmp_path):
        conv = {
            "mapping": {
                "root": {"parent": None, "message": None},
                "msg1": {
                    "parent": "root",
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["hello"]},
                    },
                },
                "msg2": {
                    "parent": "msg1",
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["hi there"]},
                    },
                },
            }
        }
        f = self._make_zip(tmp_path, [conv])
        convs, errors = parse_zip_mapping_tree(f)
        assert len(convs) == 1
        assert convs[0] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

    def test_skips_system_messages(self, tmp_path):
        conv = {
            "mapping": {
                "root": {"parent": None, "message": None},
                "sys": {
                    "parent": "root",
                    "message": {
                        "author": {"role": "system"},
                        "content": {"parts": ["You are helpful"]},
                    },
                },
                "msg1": {
                    "parent": "sys",
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["hi"]},
                    },
                },
                "msg2": {
                    "parent": "msg1",
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["hello"]},
                    },
                },
            }
        }
        f = self._make_zip(tmp_path, [conv])
        convs, errors = parse_zip_mapping_tree(f)
        assert len(convs) == 1
        assert all(t["role"] in ("user", "assistant") for t in convs[0])

    def test_multipart_content(self, tmp_path):
        conv = {
            "mapping": {
                "root": {"parent": None, "message": None},
                "msg1": {
                    "parent": "root",
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["part1", "part2"]},
                    },
                },
                "msg2": {
                    "parent": "msg1",
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["response"]},
                    },
                },
            }
        }
        f = self._make_zip(tmp_path, [conv])
        convs, errors = parse_zip_mapping_tree(f)
        assert convs[0][0]["content"] == "part1\npart2"

    def test_bad_zip(self, tmp_path):
        f = tmp_path / "bad.zip"
        f.write_bytes(b"not a zip")
        convs, errors = parse_zip_mapping_tree(f)
        assert convs == []
        assert len(errors) == 1

    def test_empty_conversations(self, tmp_path):
        f = self._make_zip(tmp_path, [])
        convs, errors = parse_zip_mapping_tree(f)
        assert convs == []
        assert errors == []


# --- parse_zip_chat_messages ---


class TestParseZipChatMessages:
    def _make_zip(self, tmp_path, conversations_data):
        f = tmp_path / "export.zip"
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("conversations.json", json.dumps(conversations_data))
        return f

    def test_basic(self, tmp_path):
        conv = {
            "chat_messages": [
                {"sender": "human", "text": "hello"},
                {"sender": "assistant", "text": "hi there"},
            ]
        }
        f = self._make_zip(tmp_path, [conv])
        convs, errors = parse_zip_chat_messages(f)
        assert len(convs) == 1
        assert convs[0] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]

    def test_multiple_conversations(self, tmp_path):
        data = [
            {
                "chat_messages": [
                    {"sender": "human", "text": "a"},
                    {"sender": "assistant", "text": "b"},
                ]
            },
            {
                "chat_messages": [
                    {"sender": "human", "text": "c"},
                    {"sender": "assistant", "text": "d"},
                ]
            },
        ]
        f = self._make_zip(tmp_path, data)
        convs, errors = parse_zip_chat_messages(f)
        assert len(convs) == 2

    def test_skips_empty_text(self, tmp_path):
        conv = {
            "chat_messages": [
                {"sender": "human", "text": ""},
                {"sender": "assistant", "text": "hello"},
            ]
        }
        f = self._make_zip(tmp_path, [conv])
        convs, errors = parse_zip_chat_messages(f)
        assert len(convs) == 1
        assert len(convs[0]) == 1  # only assistant

    def test_bad_zip(self, tmp_path):
        f = tmp_path / "bad.zip"
        f.write_bytes(b"not a zip")
        convs, errors = parse_zip_chat_messages(f)
        assert convs == []
        assert len(errors) == 1


# --- _flatten_mapping_tree ---


class TestFlattenMappingTree:
    def test_empty_mapping(self):
        assert _flatten_mapping_tree({}) == []

    def test_follows_first_child(self):
        mapping = {
            "root": {"parent": None, "message": None},
            "a": {
                "parent": "root",
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["first branch"]},
                },
            },
            "b": {
                "parent": "root",
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["second branch"]},
                },
            },
            "a_reply": {
                "parent": "a",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["reply to first"]},
                },
            },
        }
        turns = _flatten_mapping_tree(mapping)
        assert len(turns) == 2
        assert turns[0]["content"] == "first branch"
        assert turns[1]["content"] == "reply to first"


# --- parse_sqlite_chat ---


class TestParseSqliteChat:
    def _make_db(self, tmp_path, rows):
        f = tmp_path / "webui.db"
        conn = sqlite3.connect(str(f))
        conn.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, chat TEXT)")
        for row_id, chat_data in rows:
            conn.execute(
                "INSERT INTO chat (id, chat) VALUES (?, ?)",
                (row_id, json.dumps(chat_data)),
            )
        conn.commit()
        conn.close()
        return f

    def test_flat_messages(self, tmp_path):
        chat_data = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
        }
        f = self._make_db(tmp_path, [("1", chat_data)])
        convs, errors = parse_sqlite_chat(f)
        assert len(convs) == 1
        assert convs[0] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]

    def test_tree_messages_with_history(self, tmp_path):
        chat_data = {
            "messages": [
                {
                    "id": "m1",
                    "parentId": None,
                    "childrenIds": ["m2"],
                    "role": "user",
                    "content": "hello",
                },
                {
                    "id": "m2",
                    "parentId": "m1",
                    "childrenIds": ["m3"],
                    "role": "assistant",
                    "content": "hi",
                },
                {
                    "id": "m3",
                    "parentId": "m2",
                    "childrenIds": [],
                    "role": "user",
                    "content": "thanks",
                },
            ],
            "history": {
                "currentId": "m3",
                "messages": {},
            },
        }
        f = self._make_db(tmp_path, [("1", chat_data)])
        convs, errors = parse_sqlite_chat(f)
        assert len(convs) == 1
        assert len(convs[0]) == 3
        assert convs[0][0]["content"] == "hello"
        assert convs[0][2]["content"] == "thanks"

    def test_tree_follows_active_branch(self, tmp_path):
        """With branching, should follow path to history.currentId."""
        chat_data = {
            "messages": [
                {
                    "id": "m1",
                    "parentId": None,
                    "childrenIds": ["m2a", "m2b"],
                    "role": "user",
                    "content": "hello",
                },
                {
                    "id": "m2a",
                    "parentId": "m1",
                    "childrenIds": [],
                    "role": "assistant",
                    "content": "first reply",
                },
                {
                    "id": "m2b",
                    "parentId": "m1",
                    "childrenIds": [],
                    "role": "assistant",
                    "content": "regenerated reply",
                },
            ],
            "history": {
                "currentId": "m2b",
                "messages": {},
            },
        }
        f = self._make_db(tmp_path, [("1", chat_data)])
        convs, errors = parse_sqlite_chat(f)
        assert len(convs) == 1
        assert convs[0][1]["content"] == "regenerated reply"

    def test_multiple_chats(self, tmp_path):
        rows = [
            (
                "1",
                {
                    "messages": [
                        {"role": "user", "content": "a"},
                        {"role": "assistant", "content": "b"},
                    ]
                },
            ),
            (
                "2",
                {
                    "messages": [
                        {"role": "user", "content": "c"},
                        {"role": "assistant", "content": "d"},
                    ]
                },
            ),
        ]
        f = self._make_db(tmp_path, rows)
        convs, errors = parse_sqlite_chat(f)
        assert len(convs) == 2

    def test_invalid_json_row(self, tmp_path):
        f = tmp_path / "webui.db"
        conn = sqlite3.connect(str(f))
        conn.execute("CREATE TABLE chat (id TEXT PRIMARY KEY, chat TEXT)")
        conn.execute("INSERT INTO chat (id, chat) VALUES (?, ?)", ("1", "not json"))
        conn.commit()
        conn.close()
        convs, errors = parse_sqlite_chat(f)
        assert len(convs) == 0
        assert len(errors) == 1

    def test_nonexistent_db(self, tmp_path):
        f = tmp_path / "missing.db"
        convs, errors = parse_sqlite_chat(f)
        assert len(convs) == 0
        assert len(errors) == 1


# --- parse_thread_dir ---


class TestParseThreadDir:
    def test_single_thread(self, tmp_path):
        thread = tmp_path / "thread_1"
        thread.mkdir()
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        (thread / "messages.jsonl").write_text("\n".join(json.dumps(m) for m in msgs))
        convs, errors = parse_thread_dir(tmp_path)
        assert len(convs) == 1
        assert convs[0] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]

    def test_multiple_threads(self, tmp_path):
        for i in range(3):
            thread = tmp_path / f"thread_{i}"
            thread.mkdir()
            msgs = [
                {"role": "user", "content": f"q{i}"},
                {"role": "assistant", "content": f"a{i}"},
            ]
            (thread / "messages.jsonl").write_text(
                "\n".join(json.dumps(m) for m in msgs)
            )
        convs, errors = parse_thread_dir(tmp_path)
        assert len(convs) == 3

    def test_direct_thread_dir(self, tmp_path):
        """Directory itself contains messages.jsonl."""
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        (tmp_path / "messages.jsonl").write_text("\n".join(json.dumps(m) for m in msgs))
        convs, errors = parse_thread_dir(tmp_path)
        assert len(convs) == 1

    def test_nested_content_blocks(self, tmp_path):
        """Assistants API style nested content."""
        thread = tmp_path / "thread_1"
        thread.mkdir()
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": {"value": "hello", "annotations": []}}
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": {"value": "hi there", "annotations": []}}
                ],
            },
        ]
        (thread / "messages.jsonl").write_text("\n".join(json.dumps(m) for m in msgs))
        convs, errors = parse_thread_dir(tmp_path)
        assert len(convs) == 1
        assert convs[0][0] == {"role": "user", "content": "hello"}
        assert convs[0][1] == {"role": "assistant", "content": "hi there"}

    def test_standalone_file(self, tmp_path):
        """Can parse a standalone messages.jsonl file."""
        f = tmp_path / "messages.jsonl"
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        f.write_text("\n".join(json.dumps(m) for m in msgs))
        convs, errors = parse_thread_dir(f)
        assert len(convs) == 1

    def test_skips_system(self, tmp_path):
        thread = tmp_path / "thread_1"
        thread.mkdir()
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        (thread / "messages.jsonl").write_text("\n".join(json.dumps(m) for m in msgs))
        convs, errors = parse_thread_dir(tmp_path)
        assert len(convs) == 1
        assert all(t["role"] in ("user", "assistant") for t in convs[0])

    def test_bad_json_line(self, tmp_path):
        thread = tmp_path / "thread_1"
        thread.mkdir()
        (thread / "messages.jsonl").write_text(
            '{"role": "user", "content": "hi"}\n{bad json\n'
            '{"role": "assistant", "content": "hello"}'
        )
        convs, errors = parse_thread_dir(tmp_path)
        assert len(convs) == 1
        assert len(convs[0]) == 2
        assert len(errors) == 1


# --- _extract_thread_content ---


class TestExtractThreadContent:
    def test_plain_string(self):
        assert _extract_thread_content("hello") == "hello"

    def test_nested_blocks(self):
        content = [{"type": "text", "text": {"value": "hello", "annotations": []}}]
        assert _extract_thread_content(content) == "hello"

    def test_multiple_blocks(self):
        content = [
            {"type": "text", "text": {"value": "part1"}},
            {"type": "text", "text": {"value": "part2"}},
        ]
        assert _extract_thread_content(content) == "part1\npart2"

    def test_empty_list(self):
        assert _extract_thread_content([]) == ""

    def test_none(self):
        assert _extract_thread_content(None) == ""

    def test_text_as_string(self):
        content = [{"type": "text", "text": "just a string"}]
        assert _extract_thread_content(content) == "just a string"


# --- ImportResult.merge ---


class TestImportResultMerge:
    def test_merge(self):
        a = ImportResult(imported=5, skipped_empty=1, errors=["err1"])
        b = ImportResult(imported=3, skipped_duplicate=2, errors=["err2"])
        a.merge(b)
        assert a.imported == 8
        assert a.skipped_empty == 1
        assert a.skipped_duplicate == 2
        assert a.errors == ["err1", "err2"]


# --- import_conversations ---


class TestImportConversations:
    def test_dry_run(self, tmp_path):
        f = tmp_path / "data.jsonl"
        line = json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ]
            }
        )
        f.write_text(line)
        result = import_conversations(store=None, path=f, dry_run=True)
        assert result.imported == 1
        assert result.skipped_duplicate == 0

    def test_dedup(self, tmp_path):
        f = tmp_path / "data.jsonl"
        line = json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ]
            }
        )
        # Same conversation twice
        f.write_text(line + "\n" + line)

        store = MagicMock()
        # First call returns an ID, second returns None (duplicate)
        store.log_exchange_with_hash.side_effect = ["abc-123", None]

        result = import_conversations(store=store, path=f)
        assert result.imported == 1
        assert result.skipped_duplicate == 1

    def test_unknown_format(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c")
        result = import_conversations(store=None, path=f)
        assert result.imported == 0
        assert len(result.errors) == 1

    def test_format_override(self, tmp_path):
        f = tmp_path / "data.jsonl"
        line = json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                ]
            }
        )
        f.write_text(line)
        result = import_conversations(
            store=None, path=f, format=FMT_MESSAGES_JSONL, dry_run=True
        )
        assert result.imported == 1
