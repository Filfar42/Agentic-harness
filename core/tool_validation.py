"""Strict validation for the JSON Schema subset used by the tool registry.

Validation never coerces values and never repairs executable arguments. In
particular, a string ``"false"`` cannot become a truthy boolean at a tool boundary.
Limits also apply to callers that bypass JSON decoding and supply Python objects.
"""

from __future__ import annotations

import math
from typing import Any

MAX_ARGUMENT_DEPTH = 16
MAX_ARGUMENT_NODES = 4096
MAX_ARGUMENT_CHARS = 4 * 1024 * 1024
MAX_VALIDATION_ERRORS = 8


def validate_arguments(value: Any, schema: dict[str, Any]) -> list[dict[str, str]]:
    """Return bounded path/message diagnostics; an empty list means valid input."""
    errors: list[dict[str, str]] = []
    nodes = 0
    chars = 0

    def fail(path: str, message: str) -> None:
        if len(errors) < MAX_VALIDATION_ERRORS:
            errors.append({"path": path[:160], "message": message})

    def visit(item: Any, spec: dict[str, Any], path: str, depth: int) -> None:
        nonlocal nodes, chars
        if len(errors) >= MAX_VALIDATION_ERRORS:
            return
        nodes += 1
        if depth > MAX_ARGUMENT_DEPTH or nodes > MAX_ARGUMENT_NODES:
            fail(path, "Argomenti troppo complessi: riduci profondita' e numero di elementi.")
            return
        kind = spec.get("type")
        valid_type = {
            "object": type(item) is dict,
            "array": type(item) is list,
            "string": type(item) is str,
            "boolean": type(item) is bool,
            "integer": type(item) is int,
            "number": type(item) in (int, float),
            "null": item is None,
        }.get(kind, False)
        if not valid_type:
            fail(path, f"Tipo richiesto: {kind}; ricevuto: {type(item).__name__}.")
            return
        if "enum" in spec and item not in spec["enum"]:
            fail(path, "Valore ammesso: " + ", ".join(map(str, spec["enum"])))
            return
        if kind == "object":
            properties = spec.get("properties", {})
            for required in spec.get("required", []):
                if required not in item:
                    fail(f"{path}.{required}", "Parametro obbligatorio mancante.")
            if len(item) > MAX_ARGUMENT_NODES:
                fail(path, "Troppi parametri.")
                return
            for key, child in item.items():
                if type(key) is not str or key not in properties:
                    fail(f"{path}.{str(key)[:80]}", f"Parametro sconosciuto: {str(key)[:80]}.")
                    continue
                visit(child, properties[key], f"{path}.{key}", depth + 1)
        elif kind == "array":
            if not spec.get("minItems", 0) <= len(item) <= spec.get("maxItems", 128):
                fail(path, "Numero di elementi fuori dai limiti dello schema.")
                return
            for index, child in enumerate(item):
                visit(child, spec.get("items", {}), f"{path}[{index}]", depth + 1)
        elif kind == "string":
            chars += len(item)
            if chars > MAX_ARGUMENT_CHARS:
                fail(path, "Gli argomenti superano il limite complessivo di 4 MiB di testo.")
                return
            if not spec.get("minLength", 0) <= len(item) <= spec.get("maxLength", 2 * 1024 * 1024):
                fail(path, "Lunghezza fuori dai limiti dello schema.")
                return
            try:
                item.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                fail(path, "La stringa contiene un surrogato Unicode non valido.")
        elif kind in ("integer", "number"):
            if kind == "number" and not math.isfinite(item):
                fail(path, "Il numero deve essere finito.")
            elif item < spec.get("minimum", -math.inf) or item > spec.get("maximum", math.inf):
                fail(path, "Numero fuori dai limiti dello schema.")

    visit(value, schema, "$", 0)
    return errors
