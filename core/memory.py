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


def save_memories(memories: list[dict[str, str]], path: Path | None = None) -> bool:
    """Scrive le memorie. Torna False se il disco ha detto di no.

    Il ``pass`` sull'``OSError`` c'era per non far esplodere niente -- giusto --
    ma nessuno sapeva piu' com'era andata: una memoria mai arrivata sul disco
    e il modello che leggeva ``{"status": "ok"}``. La memoria a lungo termine e'
    proprio la cosa di cui l'utente si accorge del guasto **la prossima
    settimana**, quando non c'e' piu' modo di risalire a cosa e' successo.
    """
    if path is None:
        path = MEMORY_FILE
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(memories, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


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


class MemoriaAmbigua(ValueError):
    """Il riferimento corrisponde a piu' di una memoria."""

    def __init__(self, candidate: list[dict[str, str]]):
        self.candidate = candidate
        elenco = "; ".join(f"[{m['id']}] {m['text'][:50]}" for m in candidate[:4])
        super().__init__(
            f"'{len(candidate)}' memorie corrispondono: {elenco}. "
            "Cita l'id esatto per dire quale."
        )


def remove_memory(memories: list[dict[str, str]], needle: str) -> bool:
    """Rimuove per id esatto, oppure per testo esatto (case-insensitive).

    Il ripiego per **sottostringa** e' stato tolto. Cancellava la prima memoria
    che conteneva il testo citato: con "Filip preferisce le risposte brevi" e
    "Filip preferisce le risposte brevi nei riepiloghi", un ``remove`` sul testo
    della seconda cancellava la prima. Sono i fatti stabili dell'utente,
    accumulati per mesi, e la cancellazione non ha un annulla.

    Resta il match per **prefisso**, che copre il caso vero per cui il ripiego
    era stato scritto -- il modello che cita una memoria troncandola -- ma solo
    se e' univoco: se ne combaciano due, si solleva invece di indovinare.
    """
    needle_l = needle.strip().lower()
    if not needle_l:
        return False
    for idx, mem in enumerate(memories):
        if mem["id"] == needle.strip() or mem["text"].lower() == needle_l:
            memories.pop(idx)
            return True
    candidate = [m for m in memories if m["text"].lower().startswith(needle_l)]
    if len(candidate) > 1:
        raise MemoriaAmbigua(candidate)
    if candidate:
        memories.remove(candidate[0])
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
