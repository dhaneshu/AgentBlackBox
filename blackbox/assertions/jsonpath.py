"""Small, deterministic JSONPath selector used by assertions."""

from __future__ import annotations

import re
import json
from typing import Any


class JSONPathError(ValueError):
    pass


_TOKEN = re.compile(
    r"""(?:\.([A-Za-z_][A-Za-z0-9_-]*|\*))|(?:\[(\*|-?\d+|'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")\])"""
)


def _decode_quoted(raw: str) -> str:
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JSONPathError(f"invalid quoted key: {exc.msg}") from exc

    content = raw[1:-1]
    encoded: list[str] = []
    index = 0
    while index < len(content):
        char = content[index]
        if char == '"':
            encoded.append('\\"')
            index += 1
            continue
        if char != "\\":
            encoded.append(char)
            index += 1
            continue
        if index + 1 >= len(content):
            raise JSONPathError("quoted key ends with an incomplete escape")
        escaped = content[index + 1]
        if escaped == "'":
            encoded.append("'")
            index += 2
        elif escaped in {'"', "\\", "/", "b", "f", "n", "r", "t"}:
            encoded.extend(("\\", escaped))
            index += 2
        elif escaped == "u":
            digits = content[index + 2:index + 6]
            if len(digits) != 4 or not all(v in "0123456789abcdefABCDEF" for v in digits):
                raise JSONPathError("quoted key contains an invalid Unicode escape")
            encoded.extend(("\\u", digits))
            index += 6
        else:
            raise JSONPathError(f"unsupported quoted-key escape \\{escaped}")
    try:
        return json.loads(f'"{"".join(encoded)}"')
    except json.JSONDecodeError as exc:
        raise JSONPathError(f"invalid quoted key: {exc.msg}") from exc


def select(document: Any, path: str) -> list[Any]:
    """Select values for a conservative JSONPath subset: $, fields, indexes, wildcards."""
    if not path or path == "$":
        return [document]
    if not path.startswith("$"):
        raise JSONPathError("JSONPath must start with '$'")
    position, tokens = 1, []
    while position < len(path):
        match = _TOKEN.match(path, position)
        if not match:
            raise JSONPathError(f"unsupported JSONPath syntax at character {position}")
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if raw and raw[:1] in {"'", '"'}:
            raw = _decode_quoted(raw)
        tokens.append(raw)
        position = match.end()
    current = [document]
    for token in tokens:
        selected = []
        for value in current:
            if token == "*":
                if isinstance(value, dict):
                    selected.extend(value.values())
                elif isinstance(value, list):
                    selected.extend(value)
                continue
            if isinstance(value, dict) and token in value:
                selected.append(value[token])
            elif isinstance(value, (list, tuple)) and str(token).lstrip("-").isdigit():
                index = int(token)
                if -len(value) <= index < len(value):
                    selected.append(value[index])
        current = selected
    return current
