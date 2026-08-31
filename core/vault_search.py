"""vault_search: sotto-chiamata all'agente manutentore di un vault.

## Il flusso

Una chat normale consulta i vault senza aprirli come workspace: il tool
``vault_search`` prende il nome del vault e una domanda, fa girare un
sotto-turno *dentro il workspace del vault* e al chiamante torna solo il
referto con i link alle pagine usate. E' lo stesso baratto della delega --
latenza in cambio di contesto -- ma il figlio non gira sul workspace
corrente: gira sulla wiki, che e' un altro disco.

## Perche' solo lettura, anche qui

La manutenzione della wiki (ingest, query-che-diventano-pagine, lint) e'
lavoro da manutentore e ha senso solo col vault aperto come workspace,
dove l'utente vede le pagine cambiare in Obsidian mentre parli. Una chat
normale invece chiede *risposte*: se il cercatore potesse scrivere, ogni
domanda sparata da un'altra conversazione modificherebbe la wiki fuori dal
campo visivo di chi la possiede. Quindi qui si legge soltanto; per scrivere,
si apre il vault.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import vault as vault_mod

# Stessa filosofia dei tetti della delega: una ricerca nella wiki che non si
# chiude in poche mosse non e' una ricerca. L'indice tiene il primo passo
# corto, quindi il budget basta: leggere indice + 3-4 pagine e rispondere.
MAX_PASSI_CERCA = 6

# Come per la delega: oltre questa soglia il referto sposta il problema
# invece di risolverlo.
MAX_REFERTO_CHARS = 2_500

# Gli stessi tool dell'esploratore: elencare, leggere, cercare. Niente
# scrittura (il motivo sta nel docstring del modulo), niente comandi,
# niente piano, niente domande all'utente.
TOOL_CERCA = ("list_files", "read_file", "search_files")

PROMPT_CERCATORE = """\
Sei il cercatore di una wiki personale mantenuta secondo il pattern LLM Wiki \
(Obsidian). Il tuo workspace e' il vault: `wiki/` contiene le pagine generate, \
`raw/` le fonti originali, `wiki/index.md` e' il catalogo di tutto.

Hai al massimo {passi} passi e tre strumenti: elencare file, leggerli, cercare \
dentro. Non puoi scrivere niente: sei in consultazione, non in manutenzione.

Flusso prescritto:
1. parti SEMPRE da `{indice}`: e' la mappa della wiki;
2. apri solo le pagine rilevanti per la domanda;
3. rispondi SOLO con l'ultima risposta: chi ti chiama non vedra' niente altro.

Nella risposta:
- cita le pagine usate coi loro percorsi relativi (es. `wiki/Entita/X.md`) e i \
link Obsidian `[[Pagina]]` dove esistono;
- distingui cio' che dice la wiki da cio' che NON c'e': una lacuna della wiki \
e' un risultato, non un fallimento -- dillo, servira' alla manutenzione;
- dimensiona la risposta alla domanda, entro {referto} caratteri.

Se la wiki non copre la domanda, rispondi comunque: cosa hai cercato, quali \
pagine sono vicine, cosa mancherebbe da aggiungere.
"""


def _compito(query: str, info: vault_mod.VaultInfo) -> str:
    """Il messaggio utente del cercatore: domanda + stato economico del vault."""
    stato = (
        f"Vault '{info.nome}': {info.pagine} pagine in wiki/, "
        f"{info.fonti} fonti in raw/."
    )
    return f"{stato}\n\nDomanda: {query}"


def schema_ridotto(tools_schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lo schema del cercatore: solo i tre tool di consultazione."""
    return [
        t
        for t in tools_schema
        if (t.get("function") or {}).get("name") in TOOL_CERCA
    ]


def cerca_nel_vault(
    vault_nome: str,
    query: str,
    *,
    backend: Any,
    params: Any,
    tools_schema: list[dict[str, Any]],
    tool_ctx: Any,
    env_header: str | None,
    run_turn: Any,
    registri: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Esegue la ricerca nel vault e ritorna il referto.

    ``registri`` e' l'elenco dei vault noti (la chiave ``"vaults"`` delle
    impostazioni): serve a risolvere il nome in percorso. ``run_turn`` arriva
    come parametro, come nella delega, per non chiudere anelli fra moduli.
    """
    query = str(query or "").strip()
    if not query:
        return {"errore": "La query e' vuota: scrivi cosa vuoi sapere dalla wiki."}

    # Risoluzione nome -> percorso. Si accetta anche un percorso diretto:
    # costa una riga e rende il tool usabile senza passare dall'UI.
    base: Path | None = None
    nome_usato = ""
    for reg in registri or []:
        if reg.get("nome") == vault_nome or reg.get("path") == vault_nome:
            candidato = Path(str(reg.get("path", ""))).expanduser()
            if candidato.is_dir():
                base = candidato
                nome_usato = str(reg.get("nome") or candidato.name)
            break
    if base is None:
        diretto = Path(vault_nome).expanduser()
        if vault_mod.is_vault(diretto):
            base = diretto
            nome_usato = diretto.name
    if base is None:
        return {
            "errore": (
                f"Nessun vault registrato si chiama '{vault_nome}'. "
                "Registralo dalla sezione Vault o passa il percorso diretto."
            )
        }

    # La coda del log non si legge piu' qui: arriva dentro ``blocco_stato``,
    # insieme all'indice, e va in coda invece che nel prompt di sistema.
    indice = vault_mod.leggi_indice(base)
    system_prompt = PROMPT_CERCATORE.format(
        passi=MAX_PASSI_CERCA,
        indice=vault_mod.INDEX_FILE,
        referto=MAX_REFERTO_CHARS,
    )
    if not indice.strip():
        # Questa riga si', nel prompt: e' un fatto stabile sulla cartella, non
        # un contenuto che cambia.
        system_prompt += (
            f"\nL'indice (`{vault_mod.INDEX_FILE}`) e' vuoto o illeggibile: "
            "parti dagli stessi file in `wiki/`.\n"
        )
    # L'indice e la coda del log vanno **in coda**, non nel prompt di sistema.
    #
    # E' lo stesso difetto corretto in ``vault.blocco_manutenzione``, qui nel
    # sotto-agente: ``wiki/index.md`` viene riscritto a ogni ingest, e in testa
    # invalidava il prefisso del cercatore. Sul cercatore pesa meno che sul
    # padre -- il suo prefisso vive sei passi -- ma la ragione e' la stessa, e
    # due chiamate consecutive sulla stessa wiki adesso lo riusano davvero.
    blocco_stato = vault_mod.blocco_stato(base)

    # Contesto figlio: workspace DEL VAULT, non quello corrente. Niente
    # piano, note, memorie, preview: il cercatore consulta, non partecipa.
    ctx_figlio = replace(
        tool_ctx,
        workspace=str(base),
        plan=type(tool_ctx.plan)(),
        notes=type(tool_ctx.notes)(),
        on_plan_changed=None,
        on_notes_changed=None,
        on_memories_changed=None,
        read_cache={},
        new_symbols={},
        step=0,
        # Il perimetro di sola lettura come dato, non come schema: qui pesa
        # il doppio, perche' il workspace del cercatore e' la cartella di un
        # ALTRO vault dell'utente. ``schema_ridotto`` dice al modello cosa
        # esiste; le chiamate scritte come testo non lo consultano.
        tool_consentiti=frozenset(TOOL_CERCA),
        # Un cercatore non apre altri sotto-turni.
        on_delega=None,
        on_vault_search=None,
    )

    compito = _compito(query, vault_mod.info_vault(str(base), nome_usato))
    if blocco_stato:
        compito = f"{blocco_stato}\n\n{compito}"
    messaggi: list[dict[str, Any]] = [{"role": "user", "content": compito}]
    passi = 0
    for evento in run_turn(
        backend=backend,
        params=replace(params, think=False),
        tools_schema=schema_ridotto(tools_schema),
        tool_ctx=ctx_figlio,
        ui_messages=messaggi,
        system_prompt=system_prompt,
        env_header=env_header,
        max_steps=MAX_PASSI_CERCA,
    ):
        # ``StepStarted`` come in ``delega.esegui``: prima qui si contavano i
        # ``ToolFinished``, cioe' le **chiamate**, e lo stesso campo ``passi``
        # significava due cose diverse nei due referti -- un passo con tre
        # letture ne dichiarava tre.
        if evento.__class__.__name__ == "StepStarted":
            passi += 1
            continue
        if evento.__class__.__name__ == "AssistantTurn":
            testo = getattr(evento, "content", "") or ""
            if not str(testo).strip():
                continue
            referto = str(testo).strip()
            if len(referto) > MAX_REFERTO_CHARS:
                # Il marcatore, come in ``delega.esegui``: senza, il padre
                # riceve una risposta che finisce a meta' frase e non ha modo
                # di sapere che manca qualcosa -- la tratta come completa.
                referto = referto[:MAX_REFERTO_CHARS].rstrip() + "\n[referto troncato]"
            return {
                "vault": nome_usato,
                "query": query,
                "passi": passi,
                "referto": referto,
            }
    return {
        "vault": nome_usato,
        "query": query,
        "passi": passi,
        "errore": "La ricerca nel vault non ha prodotto una risposta entro il limite di passi.",
    }


def referto_compatto(esito: dict[str, Any], max_chars: int = 0) -> str:
    """Il testo che il tool rimanda al chiamante, pronto per il modello."""
    if esito.get("errore"):
        return json.dumps({"errore": esito["errore"]}, ensure_ascii=False)
    out = {
        "vault": esito.get("vault", ""),
        "passi": esito.get("passi", 0),
        "referto": esito.get("referto", ""),
    }
    tetto = max_chars or (MAX_REFERTO_CHARS + 200)
    testo = json.dumps(out, ensure_ascii=False)
    if len(testo) > tetto:
        # Si accorcia il **campo**, non la busta: tagliare il JSON serializzato
        # produce JSON invalido, e il modello riceve qualcosa che non sa
        # leggere proprio quando il referto e' abbondante.
        margine = len(testo) - len(out["referto"])
        spazio = max(0, tetto - margine - len("\n[referto troncato]"))
        out["referto"] = out["referto"][:spazio].rstrip() + "\n[referto troncato]"
        testo = json.dumps(out, ensure_ascii=False)
    return testo
