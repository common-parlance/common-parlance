"""Tests for conversation turn extraction."""

import json

from common_parlance.extract import extract_turns


def test_openai_format():
    request = json.dumps(
        {
            "model": "mistral:7b",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "What is Python?"},
            ],
        }
    )
    response = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Python is a programming language.",
                    }
                }
            ]
        }
    )

    turns = extract_turns(request, response)
    assert turns is not None
    assert len(turns) == 2  # system prompt filtered out
    assert turns[0] == {"role": "user", "content": "What is Python?"}
    assert turns[1] == {
        "role": "assistant",
        "content": "Python is a programming language.",
    }


def test_ollama_chat_format():
    request = json.dumps(
        {
            "model": "llama3.1:8b",
            "messages": [{"role": "user", "content": "Hello"}],
        }
    )
    response = json.dumps({"message": {"role": "assistant", "content": "Hi there!"}})

    turns = extract_turns(request, response)
    assert turns is not None
    assert len(turns) == 2
    assert turns[0]["role"] == "user"
    assert turns[1]["content"] == "Hi there!"


def test_ollama_generate_format():
    request = json.dumps({"model": "codellama", "prompt": "Write hello world"})
    response = json.dumps({"response": "print('Hello, world!')"})

    turns = extract_turns(request, response)
    assert turns is not None
    assert len(turns) == 2
    assert turns[0]["content"] == "Write hello world"
    assert turns[1]["content"] == "print('Hello, world!')"


def test_system_prompts_filtered():
    request = json.dumps(
        {
            "messages": [
                {"role": "system", "content": "You are a secret agent named Bob."},
                {"role": "user", "content": "Hi"},
            ]
        }
    )
    response = json.dumps({"choices": [{"message": {"content": "Hello!"}}]})

    turns = extract_turns(request, response)
    assert turns is not None
    # System prompt should be stripped
    assert all(t["role"] != "system" for t in turns)


def test_invalid_json_returns_none():
    assert extract_turns("not json", "also not json") is None


def test_empty_response_returns_none():
    request = json.dumps({"messages": [{"role": "user", "content": "Hi"}]})
    response = json.dumps({"choices": []})

    assert extract_turns(request, response) is None
