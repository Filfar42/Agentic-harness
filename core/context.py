"""Riduzione dei risultati senza spezzare JSON, esiti o riferimenti."""
from __future__ import annotations

import json
from typing import Any

from .config import Budgets


REFERENCE_KEYS = frozenset({"deposito", "stdout_deposito", "stderr_deposito", "artifact_path"})
METADATA_KEYS = frozenset({
    "status", "esito", "action", "filepath", "returncode", "match_count", "folder",
    "range", "total_lines", "truncated", "sha256", "bytes_before", "bytes_after", "lines",
    "error_code", "ok", "event_id", "count", "total_matches",
}) | REFERENCE_KEYS
_EXACT_PATH_KEYS = REFERENCE_KEYS | {"filepath", "folder"}


def clip(text: str, limit: int) -> str:
    """Limite stretto in caratteri, con testa/coda e marcatore incluso."""
    limit = max(0, limit)
    if len(text) <= limit:
        return text
    marker = "\n[...troncato...]\n"
    if limit <= len(marker):
        return marker.strip()[:limit]
    available = limit - len(marker)
    head = available * 3 // 5
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _bounded(value: Any, limit: int, depth: int = 0) -> Any:
    """Mantiene liste e oggetti; limita anche campi annidati di tool estesi."""
    if isinstance(value, str):
        return clip(value, limit)
    if isinstance(value, (dict, list)):
        if depth >= 4:
            return clip(json.dumps(value, ensure_ascii=False), limit)
        items = value.items() if isinstance(value, dict) else enumerate(value)
        result: Any = {} if isinstance(value, dict) else []
        remaining = limit
        for key, item in items:
            if remaining <= 20:
                break
            # Le chiavi provengono dai tool ma contano comunque nel budget.
            key_cost = len(str(key)) + 6 if isinstance(value, dict) else 2
            if key_cost >= remaining:
                break
            bounded = _bounded(item, max(0, remaining - key_cost), depth + 1)
            if isinstance(value, dict):
                result[str(key)] = bounded
            else:
                result.append(bounded)
            remaining -= len(json.dumps(bounded, ensure_ascii=False)) + key_cost
        return result
    return value


def compact_result(raw: str, *, full: bool, budgets: Budgets) -> str:
    """I budget tagliano i corpi, mai il JSON serializzato o gli handle.

    Un read_file recente mantiene il suo budget di lettura: non subisce un
    secondo limite, dimezzato, sulla busta. L'overhead dei metadati e degli
    escape JSON viene contato dalla successiva misura della richiesta.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return clip(raw, budgets.tool_result_max_chars if full else 400)
    if not isinstance(payload, dict):
        limit = budgets.tool_result_max_chars if full else 400
        return json.dumps(_bounded(payload, limit), ensure_ascii=False)

    kept = {}
    changed = False
    metadata_left = 1200
    for key, value in payload.items():
        if key not in METADATA_KEYS:
            continue
        # I percorsi reali restano utilizzabili; gli altri metadati non sono
        # un canale illimitato per script, messaggi o strutture annidate.
        if ((key in _EXACT_PATH_KEYS and isinstance(value, str))
                or value is None or isinstance(value, (bool, int, float))):
            bounded = value
        else:
            bounded = _bounded(value, min(metadata_left, 240))
            metadata_left = max(0, metadata_left - len(json.dumps(bounded, ensure_ascii=False)))
        kept[key] = bounded
        changed |= bounded != value
    is_read = "content" in payload and "filepath" in payload
    limit = budgets.read_file_max_chars if is_read else budgets.tool_result_max_chars
    remaining = max(0, limit) if full else 1500
    # Errori e stderr precedono gli output di successo nell'assegnazione.
    keys = sorted((key for key in payload if key not in METADATA_KEYS),
                  key=lambda key: ({"error": 0, "hint": 1, "stderr": 2,
                                    "command": 4, "source": 4, "version": 4}.get(key, 3)))
    for key in keys:
        value = payload[key]
        if not full:
            if key not in ("error", "hint", "content", "tree", "stdout", "stderr", "matches",
                           "command", "source", "version", "referto", "user_answer", "question"):
                changed = True
                continue
            allowance = min(remaining, 360 if key == "error" else 300)
        else:
            allowance = remaining
        if len(key) > 240:
            changed = True
            continue
        bounded = _bounded(value, allowance)
        kept[key] = bounded
        changed |= bounded != value
        remaining = max(0, remaining - len(json.dumps(bounded, ensure_ascii=False)))
    if not full:
        kept["_compacted"] = True
    if changed:
        kept["_context_truncated"] = True
    return json.dumps(kept, ensure_ascii=False)


def deposit_references(messages: Any) -> set[str]:
    """Handle espliciti nei risultati ancora necessari alla conversazione."""
    result: set[str] = set()
    for message in messages:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(message.get("content") or "{}")
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(payload, dict):
            result.update(value for key, value in payload.items()
                          if key in REFERENCE_KEYS and isinstance(value, str))
    return result
