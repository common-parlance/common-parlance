"""Extract conversation turns from raw API request/response JSON.

Handles both OpenAI-compatible and Ollama-native formats.
Strips all metadata — returns only human/assistant turn content.
"""

import json
import logging

logger = logging.getLogger(__name__)


def extract_turns(request_json: str, response_json: str) -> list[dict] | None:
    """Extract conversation turns from a raw exchange.

    Returns a list of {"role": ..., "content": ...} dicts,
    or None if the exchange can't be parsed.
    """
    try:
        request = json.loads(request_json)
        response = json.loads(response_json)
    except json.JSONDecodeError:
        logger.warning("Failed to parse exchange JSON")
        return None

    # Try OpenAI-compatible format first, then Ollama native.
    turns = _extract_openai(request, response)
    if turns is None:
        turns = _extract_ollama(request, response)

    if not turns:
        return None

    # Filter to only human/assistant turns, drop system prompts.
    return [
        {"role": t["role"], "content": t["content"]}
        for t in turns
        if t.get("role") in ("user", "assistant") and t.get("content")
    ]


def _extract_openai(request: dict, response: dict) -> list[dict] | None:
    """Extract from OpenAI /v1/chat/completions format."""
    messages = request.get("messages")
    if not isinstance(messages, list):
        return None

    # Get assistant response from choices.
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    assistant_msg = choices[0].get("message", {})
    if not assistant_msg.get("content"):
        return None

    turns = list(messages)
    turns.append({"role": "assistant", "content": assistant_msg["content"]})
    return turns


def _extract_ollama(request: dict, response: dict) -> list[dict] | None:
    """Extract from Ollama /api/chat or /api/generate format."""
    # /api/chat format
    messages = request.get("messages")
    if isinstance(messages, list):
        resp_message = response.get("message", {})
        if resp_message.get("content"):
            turns = list(messages)
            turns.append({"role": "assistant", "content": resp_message["content"]})
            return turns

    # /api/generate format (single prompt/response)
    prompt = request.get("prompt")
    resp_text = response.get("response")
    if prompt and resp_text:
        return [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": resp_text},
        ]

    return None
