"""Delega di una ricerca a un sotto-turno con contesto proprio.

## Cosa risolve, e perche' non lo risolve la compattazione

Nelle sessioni misurate di questo progetto una parte grossa del contesto e'
esplorazione: ``read_file`` e ``search_files`` serviti a decidere la mossa
successiva, che restano in cronologia per sempre. La potatura degli argomenti
ha tolto la duplicazione dei file *scritti* -- quelli sono su disco -- ma una
lettura esplorativa non e' duplicazione: e' testo che e' servito una volta e
continua a costare.

La compattazione lo riassume dopo. Questa strada e' migliore: quei token nel
contesto principale **non entrano proprio**. L'agente figlio apre venti file,
cerca, ragiona, e al padre torna solo il referto.

## Perche' solo in lettura

Un sotto-turno che scrive e' un agente autonomo, e su un modello da 27B
significa lavoro fatto in un contesto che nessuno ha rivisto, con guardie che
non possono controllare la coerenza con quello che sta facendo il padre. In
lettura invece il compito e' chiuso -- "trova dove", "dimmi se", "elenca
quali" -- e sbagliare costa un referto sbagliato, non un file corrotto.

## Cosa costa

Su una macchina sola e' seriale: la delega non e' parallelismo, e' latenza in
cambio di contesto. Lo stesso baratto della compattazione, con la differenza
che qui si paga *prima* di riempire la finestra invece che dopo.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

# Tetto ai passi del figlio. Basso di proposito: una ricerca che non si chiude
# in sei mosse non e' una ricerca, e' un compito -- e un compito va nel piano
# del padre, dove si vede, non dentro una scatola nera.
MAX_PASSI_DELEGA = 6

# Tetto al referto. Se torna piu' di cosi', la delega ha spostato il problema
# invece di risolverlo.
MAX_REFERTO_CHARS = 2_000

# I soli tool che il figlio riceve. Niente scrittura, niente run_command
# (che scrive eccome), niente piano, niente domande all'utente: il figlio non
# ha con chi parlare.
TOOL_DELEGA = ("list_files", "read_file", "search_files")

PROMPT_DELEGA = """\
Sei un esploratore. Ricevi una domanda su un workspace e hai pochi passi per \
rispondere, con soli tre strumenti: elencare file, leggerli, cercare dentro.

Non puoi scrivere niente e non puoi eseguire comandi: se la risposta \
richiedesse di modificare qualcosa, dillo invece di provarci.

Chi ti ha chiamato non vedra' niente di quello che leggi: vedra' **solo la tua \
ultima risposta**. Quindi quella deve reggersi da sola -- percorsi completi, \
numeri di riga, il frammento esatto quando serve. "Ho trovato la funzione" non \
serve a nessuno; "``budgets_for`` sta in core/config.py:105 e viene usata in \
core/agent.py:945 e core/tools.py:112" si'.

Sii breve. Se la risposta non c'e', dillo con quello che hai escluso: e' \
un'informazione anche quella.
"""


def schema_ridotto(tools_schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lo schema del figlio: gli stessi tool del padre, filtrati."""
    return [
        t
        for t in tools_schema
        if (t.get("function") or {}).get("name") in TOOL_DELEGA
    ]


def esegui(
    compito: str,
    *,
    backend: Any,
    params: Any,
    tools_schema: list[dict[str, Any]],
    tool_ctx: Any,
    env_header: str | None,
    run_turn: Any,
) -> dict[str, Any]:
    """Esegue il sotto-turno e restituisce il referto.

    ``run_turn`` arriva come parametro invece che come import per non chiudere
    un anello fra i moduli: e' ``agent`` a conoscere questo file, non il
    contrario.
    """
    compito = str(compito or "").strip()
    if not compito:
        return {"errore": "Il compito e' vuoto: scrivi cosa vuoi sapere."}

    # Contesto figlio: stesso workspace e stesse regole, ma senza il piano,
    # le note e la cronologia del padre. E' tutto il punto -- se ereditasse il
    # contesto, delegare non risparmierebbe niente.
    ctx_figlio = replace(
        tool_ctx,
        plan=type(tool_ctx.plan)(),
        notes=type(tool_ctx.notes)(),
        on_plan_changed=None,
        on_notes_changed=None,
        on_memories_changed=None,
        read_cache={},
        new_symbols={},
        step=0,
    )

    messaggi: list[dict[str, Any]] = [{"role": "user", "content": compito}]
    passi = 0
    for evento in run_turn(
        backend=backend,
        params=replace(params, think=False),
        tools_schema=schema_ridotto(tools_schema),
        tool_ctx=ctx_figlio,
        ui_messages=messaggi,
        system_prompt=PROMPT_DELEGA,
        env_header=env_header,
        max_steps=MAX_PASSI_DELEGA,
        # Tutte le reti di sicurezza del padre qui sono rumore: non c'e' un
        # piano da pretendere, non c'e' niente da verificare, non c'e' nessuno
        # a cui chiedere, e la finestra del figlio non fa in tempo a riempirsi.
        require_summary=False,
        require_plan=False,
        think_watchdog=False,
        auto_preview=False,
        compact_history=False,
        plan_gate=False,
        enable_nudge=False,
        abilita_delega=False,
    ):
        if type(evento).__name__ == "StepStarted":
            passi += 1

    referto = ""
    for msg in reversed(messaggi):
        if msg.get("role") == "assistant":
            from .textutils import strip_think

            testo = strip_think(str(msg.get("content") or "")).strip()
            if testo:
                referto = testo
                break

    letti = sorted(
        {
            (m.get("args") or {}).get("filepath", "")
            for m in messaggi
            if m.get("role") == "tool" and m.get("name") == "read_file"
        }
        - {""}
    )
    if not referto:
        return {
            "errore": "L'esplorazione non ha prodotto una risposta.",
            "passi": passi,
            "hint": "Riformula il compito in modo piu' stretto, o cerca da solo.",
        }
    return {
        "referto": referto[:MAX_REFERTO_CHARS],
        "passi": passi,
        # Dire cosa ha guardato serve al padre per fidarsi -- o per non fidarsi:
        # un referto sicuro di se' prodotto senza aprire niente e' un referto
        # inventato, e questo campo e' l'unico modo di accorgersene.
        "file_letti": letti,
    }
