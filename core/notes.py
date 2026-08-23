"""Foglio di note dell'agente: cosa ha capito, non cosa deve fare.

Il piano (``core/plan.py``) risponde a "a che punto sono"; queste note
rispondono a "cosa so". Sono due domande diverse e tenerle separate evita che
il piano si riempia di scoperte, diventando illeggibile proprio quando serve.

Esistono per la compattazione. Quando la cronologia viene riassunta e i
messaggi vecchi spariscono, tutto cio' che il modello aveva capito lungo la
strada -- che quel test fallisce per via del PYTHONPATH, che la porta 8200 e'
occupata da un processo orfano, che l'utente ha gia' detto di non toccare la
cartella legacy -- se ne andrebbe con loro. Il riassunto automatico ne salva
una parte, ma lo scrive una chiamata sola alla fine, quando il dettaglio e'
gia' sepolto sotto venti passi. Una nota scritta *nel momento* in cui la cosa
si scopre e' l'unica versione che non si perde.

Rispetto alla memoria a lungo termine (``core/memory.py``) la differenza e' la
durata: le memorie valgono per il progetto e sopravvivono a tutte le sessioni,
queste note valgono per il compito in corso e muoiono con esso. Una nota su
"il bug era un off-by-one nel parser" non serve a nessuno fra due settimane;
saperlo fra due passi puo' valere mezz'ora.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Tetti volutamente stretti. Le note vivono nel blocco di coda e vengono
# rispedite ad **ogni** passo: un foglio da tremila token si ripagherebbe solo
# se sostituisse altrettanto contesto, e non e' quello che fa. Il vincolo e'
# anche pedagogico -- costringe a scrivere la conclusione invece del racconto.
MAX_NOTES = 20
MAX_NOTE_CHARS = 240


class NoteError(ValueError):
    """Uso non valido del foglio di note."""


@dataclass(slots=True)
class Note:
    id: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text}


@dataclass(slots=True)
class Notes:
    notes: list[Note] = field(default_factory=list)
    _next_id: int = 1

    def __bool__(self) -> bool:
        return bool(self.notes)

    def __len__(self) -> int:
        return len(self.notes)

    # -- lettura ----------------------------------------------------------

    def to_list(self) -> list[dict[str, Any]]:
        return [n.to_dict() for n in self.notes]

    def find(self, riferimento: str) -> Note | None:
        """Cerca per id o per testo. Il modello sbaglia spesso il primo."""
        riferimento = (riferimento or "").strip()
        if not riferimento:
            return None
        if riferimento.isdigit():
            cercato = int(riferimento)
            for n in self.notes:
                if n.id == cercato:
                    return n
        for n in self.notes:
            if n.text == riferimento:
                return n
        # Ultimo tentativo: prefisso univoco. Un modello che cita una nota a
        # memoria la tronca, e rifiutare per una virgola mancante costa un
        # round-trip per niente.
        candidati = [n for n in self.notes if n.text.startswith(riferimento[:40])]
        return candidati[0] if len(candidati) == 1 else None

    # -- scrittura --------------------------------------------------------

    def add(self, text: str) -> Note:
        text = " ".join((text or "").split())
        if not text:
            raise NoteError("La nota e' vuota.")
        if len(text) > MAX_NOTE_CHARS:
            text = text[: MAX_NOTE_CHARS - 1].rstrip() + "…"
        for n in self.notes:
            if n.text == text:
                # Riscrivere la stessa nota e' un sintomo di un modello che
                # gira a vuoto: non lo si premia con una riga in piu'.
                return n
        if len(self.notes) >= MAX_NOTES:
            raise NoteError(
                f"Il foglio e' pieno ({MAX_NOTES} note). Togli quelle superate "
                "con action='remove' prima di aggiungerne altre."
            )
        nota = Note(id=self._next_id, text=text)
        self._next_id += 1
        self.notes.append(nota)
        return nota

    def remove(self, riferimento: str) -> Note:
        nota = self.find(riferimento)
        if nota is None:
            raise NoteError(f"Nessuna nota corrisponde a '{riferimento}'.")
        self.notes.remove(nota)
        return nota

    def clear(self) -> int:
        quante = len(self.notes)
        self.notes.clear()
        return quante

    # -- persistenza ------------------------------------------------------

    @classmethod
    def from_list(cls, raw: Any) -> Notes:
        notes: list[Note] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and str(item.get("text") or "").strip():
                    try:
                        ident = int(item.get("id") or 0)
                    except (TypeError, ValueError):
                        ident = 0
                    notes.append(Note(id=ident or len(notes) + 1, text=str(item["text"])))
                elif isinstance(item, str) and item.strip():
                    notes.append(Note(id=len(notes) + 1, text=item.strip()))
        prossimo = max((n.id for n in notes), default=0) + 1
        return cls(notes=notes[:MAX_NOTES], _next_id=prossimo)


def render_block(notes: Notes) -> str:
    """Blocco di contesto con le note, da mettere **in coda**.

    Stessa ragione del piano: il prefisso (system + environment) resta
    byte-identico fra un passo e l'altro perche' e' cio' che rende riusabile il
    KV cache, e le note cambiano proprio mentre il modello lavora. In coda la
    divergenza cade dove il contesto stava cambiando comunque.
    """
    if not notes:
        return ""
    righe = [f"- {n.id}. {n.text}" for n in notes.notes]
    return "\n".join(
        [
            "<note_di_lavoro>",
            *righe,
            "</note_di_lavoro>",
            "",
            "Sono note tue, scritte nei passi precedenti. La cronologia "
            "dettagliata puo' essere stata compattata: quello che c'e' scritto "
            "qui vale come se l'avessi appena letto.",
        ]
    )
