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

# --- economia del sotto-turno -----------------------------------------------
#
# Il modo piu' costoso di fallire della delega, osservato in sessione: il
# figlio ereditava i budget pieni del padre (read_file fino a 12.000 caratteri
# a passo), riempiva la propria finestra con quattro o cinque letture integrali
# e finiva i passi senza referto -- il padre pagava la latenza e non riceveva
# niente. Le due manopole agiscono in direzioni diverse e vanno tenute distinte:
#
#   * NUM_CTX: il margine. La finestra del figlio e' piu' larga del 50% di
#     quella del padre: e' VRAM spesa bene perche' il KV cache del figlio vive
#     mentre quello del padre e' gia' caricato, ma e' seriale su una macchina
#     sola e il +50% e' moderato apposta.
#   * BUDGET: la leva vera. Ogni singolo risultato di tool del figlio viene
#     troncato a meta' di quanto verrebbe per il padre. Un esploratore non ha
#     bisogno di file interi: gli servono percorsi, numeri di riga, frammenti.
#     La matematica risultante su una finestra padre da 16k: figlio a 24k,
#     read_file a 6.000 caratteri -- sei passi pieni entrano con margine.
FATTORE_NUM_CTX_FIGLIO = 1.5
FATTORE_BUDGET_FIGLIO = 0.5

# Sotto questi valori i budget stretti non scendono: su una finestra padre
# piccola dimezzare ancora renderebbe i frammenti troppo corti per dire
# qualcosa, e un'esplorazione che non legge abbastanza e' tempo perso uguale.
MIN_READ_FILE_CHARS = 2_500
MIN_TOOL_RESULT_CHARS = 1_500

# La finestra dei risultati integrali del figlio non scala col contesto: tre
# passi indietro gli bastano, e ogni risultato in piu' conservato intero e'
# esattamente il contesto che stiamo cercando di non spendere.
PASSI_INTEGRALI_FIGLIO = 3


def parametri_figlio(params: Any) -> Any:
    """Parametri di generazione del sotto-turno: stesso modello, piu' margine."""
    num_ctx = int(getattr(params, "num_ctx", 0) or 0)
    if num_ctx <= 0:
        return replace(params, think=False)
    return replace(params, think=False, num_ctx=int(num_ctx * FATTORE_NUM_CTX_FIGLIO))


def budget_stretti(budgets: Any) -> Any:
    """Budget di troncamento del figlio: risultati dimezzati, minimi garantiti."""
    return replace(
        budgets,
        read_file_max_chars=max(
            MIN_READ_FILE_CHARS, int(budgets.read_file_max_chars * FATTORE_BUDGET_FIGLIO)
        ),
        tool_result_max_chars=max(
            MIN_TOOL_RESULT_CHARS, int(budgets.tool_result_max_chars * FATTORE_BUDGET_FIGLIO)
        ),
        command_stdout_max_chars=max(
            MIN_TOOL_RESULT_CHARS,
            int(budgets.command_stdout_max_chars * FATTORE_BUDGET_FIGLIO),
        ),
        command_stderr_max_chars=int(
            budgets.command_stderr_max_chars * FATTORE_BUDGET_FIGLIO
        ),
        # Le liste gia' crescono piu' piano dei testi: qui basta la meta'.
        list_files_max_entries=max(30, int(budgets.list_files_max_entries // 2)),
        search_max_matches=max(10, int(budgets.search_max_matches // 2)),
        tool_result_full_window=PASSI_INTEGRALI_FIGLIO,
    )


# I numeri dichiarati nel prompt del figlio si ricavano applicando
# ``budget_stretti`` alla taratura di riferimento: una sola fonte, cosi' il
# testo promesso al modello e il troncamento realmente applicato non possono
# divergere quando qualcuno ritocca un fattore.
_BUDGET_FIGLIO_RIFERIMENTO = None


def budget_riferimento() -> Any:
    """Il budget stretto calcolato sulla taratura base (finestra padre 16k)."""
    global _BUDGET_FIGLIO_RIFERIMENTO
    if _BUDGET_FIGLIO_RIFERIMENTO is None:
        from .config import BASE_NUM_CTX, budgets_for

        _BUDGET_FIGLIO_RIFERIMENTO = budget_stretti(budgets_for(BASE_NUM_CTX))
    return _BUDGET_FIGLIO_RIFERIMENTO


MAX_LETTURA_FIGLIO = int(budget_riferimento().read_file_max_chars)
MAX_MATCHES_FIGLIO = int(budget_riferimento().search_max_matches)


PROMPT_DELEGA = f"""\
Sei un esploratore. Ricevi una domanda su un workspace e hai al massimo \
{MAX_PASSI_DELEGA} passi per rispondere, con soli tre strumenti: elencare file, \
leggerli, cercare dentro.

Non puoi scrivere niente e non puoi eseguire comandi: se la risposta \
richiedesse di modificare qualcosa, dillo invece di provarci.

Ogni lettura ti arriva **troncata ai primi {MAX_LETTURA_FIGLIO} caratteri** e le \
ricerche a {MAX_MATCHES_FIGLIO} corrispondenze: non sono un difetto, sono il tuo \
budget -- sei passi cosi' stretti ci stanno tutti nella finestra, con margine per \
la risposta finale. Non rileggere lo stesso file sperando nel resto: punta dritto \
con search_files su nomi di simboli o pattern precisi, poi read_file solo sul \
file che hai davvero identificato.

Chi ti ha chiamato non vedra' niente di quello che leggi: vedra' **solo la tua \
ultima risposta**, e solo i suoi primi {MAX_REFERTO_CHARS} caratteri -- oltre si \
tronca a meta' frase senza che nessuno lo sappia. Dimensiona quindi la risposta \
alla domanda, non al lavoro fatto: per un elenco, una riga per voce con nome e \
numero di riga; per una localizzazione, il percorso esatto con la riga. Percorsi \
completi sempre: "Ho trovato la funzione" non serve a nessuno; "``budgets_for`` \
sta in core/config.py:105 e viene usata in core/agent.py:945 e core/tools.py:112" \
si'.

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

    # Parametri e budget del figlio: NON quelli del padre. Ereditarli era il
    # modo piu' costoso di fallire -- vedi il commento a FATTORE_BUDGET_FIGLIO.
    # La finestra si allarga un poco (margine), i risultati si dimezzano (leva):
    # sei passi pieni devono entrare nella finestra *e* lasciare spazio al
    # referto, che e' l'unica cosa che il padre riceve.
    params_figlio = parametri_figlio(params)
    budgets_padre = getattr(tool_ctx, "budgets", None)
    if budgets_padre is None:
        from .config import budgets_for

        budgets_padre = budgets_for(int(getattr(params_figlio, "num_ctx", 0) or 0))

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
        budgets=budget_stretti(budgets_padre),
    )

    messaggi: list[dict[str, Any]] = [{"role": "user", "content": compito}]
    passi = 0
    for evento in run_turn(
        backend=backend,
        params=params_figlio,
        tools_schema=schema_ridotto(tools_schema),
        tool_ctx=ctx_figlio,
        ui_messages=messaggi,
        system_prompt=PROMPT_DELEGA,
        env_header=env_header,
        max_steps=MAX_PASSI_DELEGA,
        # I budget stretti vanno passati qui, non solo nel ctx: run_turn
        # ricalcola budgets_for(num_ctx) e sovrascriverebbe tool_ctx.budgets
        # al primo passo, annullando la strettata.
        budgets=budget_stretti(budgets_padre),
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

    from .textutils import strip_think

    referto = ""
    for msg in reversed(messaggi):
        if msg.get("role") == "assistant":
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
    # L'esploratore si e' fermato perche' il tetto dei passi l'ha chiuso a
    # meta': passi bruciati senza risposta, e la causa va detta al padre.
    esaurito = passi >= MAX_PASSI_DELEGA

    if not referto:
        return {
            "errore": (
                "L'esplorazione non ha prodotto una risposta"
                + (
                    f" -- ha esaurito i {MAX_PASSI_DELEGA} passi a meta' lavoro."
                    if esaurito
                    else "."
                )
            ),
            "passi": passi,
            # Qui servono soprattutto per riformulare dal punto in cui il figlio
            # si e' fermato: la domanda successiva parte da dove era arrivata lui.
            "file_letti": letti,
            "esaurito": esaurito,
            "hint": (
                "Riformula il compito piu' stretto partendo da 'file_letti', o cerca da solo."
                if esaurito
                else "Riformula il compito in modo piu' stretto, o cerca da solo."
            ),
        }

    # Il troncamento deve essere *visibile*: senza marker il padre non sa di
    # avere informazioni incomplete e rifà da capo tutto il lavoro -- la delega
    # costa due volte invece di una.
    out = {
        "passi": passi,
        # Dire cosa ha guardato serve al padre per fidarsi -- o per non fidarsi:
        # un referto sicuro di se' prodotto senza aprire niente e' un referto
        # inventato, e questo campo e' l'unico modo di accorgersene.
        "file_letti": letti,
    }
    if len(referto) > MAX_REFERTO_CHARS:
        out["referto"] = (
            referto[:MAX_REFERTO_CHARS]
            + f"\n\n[referto troncato: era lungo {len(referto)} caratteri e si "
              "chiude a meta' frase -- per il resto riformula la domanda piu' stretta]"
        )
        out["troncato"] = True
    else:
        out["referto"] = referto
    if esaurito:
        # Risposta c'e', ma e' arrivata col passo 6 gia' speso: il padre deve
        # poterla leggere come "probabilmente incompleta" e riformulare.
        out["esaurito"] = True
    return out
