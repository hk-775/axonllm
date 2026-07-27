"""OpenAI ⇄ Gemini tool translation, shared by the Google AI and Vertex adapters.

Both providers speak the identical Gemini dialect (``contents`` + ``parts``,
``functionDeclarations``, ``functionCall``/``functionResponse`` parts), so the
translation lives here once rather than being duplicated — and diverging — in
two adapters.

    OpenAI                                Gemini
    tools[].function.{name,parameters}    tools[0].functionDeclarations[]
    assistant.tool_calls[]                parts[{functionCall:{name,args}}]
    role:"tool" message                   role:"user" parts[{functionResponse}]
    finish_reason:"tool_calls"            finishReason:"STOP" + a functionCall part

Note the last row: Gemini does *not* signal tool use in ``finishReason`` — it
returns STOP and puts a functionCall part in the content. So the presence of the
part is the only signal, and callers branching on ``finish_reason`` need it
synthesized for them.
"""

from __future__ import annotations

import json
from typing import Any

# Gemini rejects unknown JSON Schema keys outright rather than ignoring them, so
# a schema written for OpenAI has to be filtered rather than passed through.
_ALLOWED_SCHEMA_KEYS = frozenset({
    "type", "format", "description", "nullable", "enum", "maxItems", "minItems",
    "properties", "required", "items", "example",
})


def _clean_schema(node: Any) -> Any:
    """Strip JSON Schema keys Gemini doesn't accept, recursively.

    ``additionalProperties``, ``$schema``, ``title``, ``default`` and friends are
    ordinary in a tool schema written for OpenAI and cause a 400 here. Dropping
    them keeps the tool usable; keeping them fails the whole request.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k not in _ALLOWED_SCHEMA_KEYS:
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {pk: _clean_schema(pv) for pk, pv in v.items()}
            elif k == "items":
                out[k] = _clean_schema(v)
            else:
                out[k] = v
        return out
    return node


def openai_tools_to_gemini(tools: list[dict]) -> list[dict]:
    """Convert OpenAI tool specs into Gemini's single-entry tools list."""
    declarations = []
    for t in tools:
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        schema = fn.get("parameters") or t.get("input_schema") or {"type": "object", "properties": {}}
        declarations.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "parameters": _clean_schema(schema),
        })
    # Gemini takes one tools entry holding all declarations, not one per tool.
    return [{"functionDeclarations": declarations}]


def openai_tool_choice_to_gemini(choice: str | dict | None) -> dict | None:
    """Map OpenAI's tool_choice onto Gemini's toolConfig.

    Gemini: AUTO | ANY | NONE, with an optional allowlist for a named function.
    """
    if choice is None:
        return None
    if choice == "none":
        return {"functionCallingConfig": {"mode": "NONE"}}
    if choice == "auto":
        return {"functionCallingConfig": {"mode": "AUTO"}}
    if choice in ("required", "any"):
        return {"functionCallingConfig": {"mode": "ANY"}}
    if isinstance(choice, dict):
        name = (choice.get("function") or {}).get("name") or choice.get("name")
        if name:
            return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}
    return None


def _parse_args(raw: Any) -> dict:
    """OpenAI sends tool arguments as a JSON string; Gemini wants an object.

    A model can emit malformed JSON, which must not fail the request — send an
    empty object and let the tool report the bad call.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except (ValueError, TypeError):
            return {}
    return raw or {}


def openai_msg_to_gemini(msg: dict) -> dict | None:
    """Convert one OpenAI-shaped message to a Gemini ``contents`` entry.

    Returns None for a message the caller should skip (a system message, which
    Gemini carries in ``systemInstruction`` instead).
    """
    role = msg.get("role", "user")
    if role == "system":
        return None

    # A tool result comes back as a user-role functionResponse part. Gemini keys
    # it by function *name*, not by call id — so a parallel call to two different
    # tools stays unambiguous, but two calls to the same tool do not. That's
    # Gemini's model, not something the adapter can fix.
    if role == "tool":
        content = msg.get("content")
        return {
            "role": "user",
            "parts": [{"functionResponse": {
                "name": msg.get("name", ""),
                "response": {"content": content if isinstance(content, str)
                             else json.dumps(content)},
            }}],
        }

    tool_calls = msg.get("tool_calls")
    if role == "assistant" and tool_calls:
        parts: list[dict] = []
        text = msg.get("content")
        if isinstance(text, str) and text:
            parts.append({"text": text})
        for tc in tool_calls:
            fn = tc.get("function") or {}
            parts.append({"functionCall": {
                "name": fn.get("name", tc.get("name", "")),
                "args": _parse_args(fn.get("arguments", tc.get("arguments", {}))),
            }})
        return {"role": "model", "parts": parts}

    content = msg.get("content")
    return {
        "role": "model" if role == "assistant" else "user",
        "parts": [{"text": content if isinstance(content, str) else str(content or "")}],
    }


def gemini_parts_to_tool_calls(parts: list[dict]) -> list[dict]:
    """Extract OpenAI-shaped tool_calls from Gemini response parts.

    Gemini returns no call id, so one is synthesized. The id only has to be
    stable within the one round-trip — the caller echoes it back in the tool
    result, and Gemini matches on function name anyway.
    """
    calls = []
    for i, part in enumerate(parts):
        fc = part.get("functionCall")
        if not fc:
            continue
        calls.append({
            "id": f"call_{fc.get('name', 'fn')}_{i}",
            "type": "function",
            "function": {
                "name": fc.get("name", ""),
                # OpenAI carries arguments as a JSON string; callers json.loads it.
                "arguments": json.dumps(fc.get("args", {})),
            },
        })
    return calls
