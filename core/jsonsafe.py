"""Strict, bounded JSON decoding at untrusted model and storage boundaries.

JSON is never repaired: guessing an escape or dropping a duplicate key can
change the meaning of a write or command. Callers return validation errors to
the model and request a fresh native tool call instead.
"""

from __future__ import annotations

import json
import math
from typing import Any

MAX_JSON_CHARS = 1_048_576
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 10_000


class JsonBoundaryError(ValueError):
    """The input is not a bounded, unambiguous JSON object."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonBoundaryError(f"Duplicate JSON key: {key[:80]}")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise JsonBoundaryError(f"Non-finite JSON number: {value}")


def loads_object(
    raw: Any, *, max_chars: int = MAX_JSON_CHARS, max_nodes: int = MAX_JSON_NODES
) -> dict[str, Any]:
    """Decode exactly one object, rejecting coercions and resource abuse.

Already decoded dictionaries are checked too. Booleans remain booleans;
schema validation belongs to the tool dispatcher. No markdown fences,
trailing commas, duplicate keys, infinities or unpaired surrogates are accepted.
    """
    if isinstance(raw, str):
        if len(raw) > max_chars:
            raise JsonBoundaryError("JSON exceeds the size limit")
        try:
            value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
        except (ValueError, RecursionError) as exc:
            raise JsonBoundaryError(str(exc)[:240]) from exc
    elif isinstance(raw, dict):
        value = raw
    else:
        raise JsonBoundaryError("Expected a JSON object")
    if not isinstance(value, dict):
        raise JsonBoundaryError("Expected a JSON object")
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    chars = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > MAX_JSON_DEPTH:
            raise JsonBoundaryError("JSON exceeds the nesting or item limit")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise JsonBoundaryError("JSON keys must be strings")
                stack.extend(((key, depth + 1), (child, depth + 1)))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            chars += len(item)
            if chars > max_chars or any(0xD800 <= ord(c) <= 0xDFFF for c in item):
                raise JsonBoundaryError("JSON string exceeds limits or contains a surrogate")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise JsonBoundaryError("JSON numbers must be finite")
        elif item is not None and not isinstance(item, (int, bool)):
            raise JsonBoundaryError("Unsupported JSON value")
    return value
