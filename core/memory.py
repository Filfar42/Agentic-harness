"""Memoria a lungo termine dell'agente.

Struttura a lista di dict (``{id, text, created_at}``) invece della lista di
stringhe usata prima: consente cancellazione stabile per id anche quando la UI
rerunna e gli indici scivolano.
Retro-compatibile con il vecchio formato (lista di stringhe).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import MEMORY_FILE

MAX_MEMORIES = 60
MAX_MEMORY_CHARS = 400


def _normalise(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str) and item.strip():
            out.append(
                {
                    "id": uuid.uuid4().hex[:8],
                    "text": item.strip(),
                    "created_at": "",
                }
            )
        elif isinstance(item, dict) and str(item.get("text", "")).strip():
            out.append(
                {
                    "id": str(item.get("id") or uuid.uuid4().hex[:8]),
                    "text": str(item["text"]).strip(),
                    "created_at": str(item.get("created_at", "")),
                }
            )
    return out


def load_memories(path: Path | None = None) -> list[dict[str, str]]:
    # Risolto a chiamata, non a definizione di default: un test che punta il
    # modulo su un altro file (conftest) deve poterlo fare davvero.
    if path is None:
        path = MEMORY_FILE
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            return _normalise(json.load(fh))
    except (OSError, json.JSONDecodeError):
        return []


def save_memories(memories: list[dict[str, str]], path: Path | None = None) -> None:
    if path is None:
        path = MEMORY_FILE
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(memories, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def add_memory(memories: list[dict[str, str]], text: str) -> tuple[bool, str]:
    text = " ".join(text.split())[:MAX_MEMORY_CHARS].strip()
    if not text:
        return False, "Memoria vuota."
    if any(m["text"].lower() == text.lower() for m in memories):
        return False, "Memoria gia' presente."
    if len(memories) >= MAX_MEMORIES:
        return False, f"Limite di {MAX_MEMORIES} memorie raggiunto."
    memories.append(
        {
            "id": uuid.uuid4().hex[:8],
            "text": text,
            "created_at": datetime.now().isoformat(timespec="minutes"),
        }
    )
    return True, text


def remove_memory(memories: list[dict[str, str]], needle: str) -> bool:
    """Rimuove per id esatto, oppure per testo (match case-insensitive)."""
    needle_l = needle.strip().lower()
    for idx, mem in enumerate(memories):
        if mem["id"] == needle.strip() or mem["text"].lower() == needle_l:
            memories.pop(idx)
            return True
    # fallback: match parziale, utile quando il modello cita la memoria a memoria
    for idx, mem in enumerate(memories):
        if needle_l and needle_l in mem["text"].lower():
            memories.pop(idx)
            return True
    return False


def format_for_prompt(memories: list[dict[str, str]]) -> str:
    if not memories:
        return ""
    lines = "\n".join(f"- [{m['id']}] {m['text']}" for m in memories)
    return (
        "\n\n<memorie_a_lungo_termine>\n"
        "Fatti stabili appresi in sessioni precedenti. Trattali come vincoli, "
        "non come suggerimenti.\n"
        f"{lines}\n"
        "</memorie_a_lungo_termine>"
    )
