"""Segnali letti nel testo: chiamate scritte a mano, domande, promesse, richieste.

Stavano in ``core/agent.py`` insieme al ciclo. Sono euristiche pure sul testo
-- nessuno stato, nessun I/O -- e le usano sia il ciclo sia le reti di
sicurezza (``core/ciclo/reti.py``): qui possono stare in un modulo che non
conosce il ciclo, e ``agent`` le ri-esporta con gli stessi nomi di prima.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..jsonsafe import JsonBoundaryError, loads_object
from ..textutils import strip_think
from ..tools import ASK_USER_TOOL, TOOL_NAMES

# ---------------------------------------------------------------------------
# Parser di fallback (sostituisce il vecchio parser a regex)
# ---------------------------------------------------------------------------

REQUIRED_ARGS: dict[str, set[str]] = {
    "read_file": {"filepath"},
    "write_file": {"filepath", "content"},
    "edit_file": {"filepath", "old_string"},
    "search_files": {"pattern"},
    "run_command": {"command"},
    "manage_memory": {"action"},
    "list_files": set(),
    ASK_USER_TOOL: {"question"},
}

_DECODER = json.JSONDecoder()


def extract_json_objects(text: str) -> list[tuple[int, int, dict[str, Any]]]:
    """Trova tutti gli oggetti JSON di primo livello in un testo.

    Usa ``JSONDecoder.raw_decode`` invece di una regex a graffe bilanciate.
    Motivo: una regex non puo' distinguere le graffe *di struttura* da quelle
    *dentro una stringa*. Il caso reale che rompeva il recupero era una
    ``write_file`` il cui ``content`` conteneva codice Python::

        {"name": "write_file", "arguments": {"filepath": "x.py",
         "content": "shopping_lists = {}\\n..."}}

    Le graffe di ``{}`` nel codice facevano fallire il match, il recupero non
    scattava e il JSON finiva stampato in chat come testo. ``raw_decode``
    conosce la grammatica JSON, quindi gestisce annidamento, stringhe con
    graffe, escape e virgolette senza casi particolari.

    Restituisce ``(inizio, fine, oggetto)`` per ogni oggetto trovato, in ordine.
    """
    results: list[tuple[int, int, dict[str, Any]]] = []
    if len(text) > 1_048_576:
        return results
    index = 0
    length = len(text)
    attempts = 0
    while index < length:
        attempts += 1
        if attempts > 128:
            break
        start = text.find("{", index)
        if start == -1:
            break
        try:
            obj, end = _DECODER.raw_decode(text, start)
        except (ValueError, RecursionError):
            index = start + 1
            continue
        if isinstance(obj, dict):
            try:
                obj = loads_object(text[start:end])
            except JsonBoundaryError:
                index = end
                continue
            results.append((start, end, obj))
            index = end
        else:
            index = start + 1
    return results


def _as_tool_call(data: dict[str, Any], seq: int) -> dict[str, Any] | None:
    """Normalizza un dict JSON in una tool call, o ``None`` se non lo e'.

    Accetta le forme che i modelli locali producono davvero:
      ``{"name": ..., "arguments": {...}}``
      ``{"name": ..., "parameters": {...}}``
      ``{"function": {"name": ..., "arguments": {...}}}``
      ``{"tool": ..., "args": {...}}``
    """
    inner = data.get("function")
    if isinstance(inner, dict):
        data = inner

    name = data.get("name") or data.get("tool") or data.get("tool_name")
    if not isinstance(name, str) or name not in TOOL_NAMES:
        return None

    args: Any = data.get("arguments")
    if args is None:
        args = data.get("parameters")
    if args is None:
        args = data.get("args")
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = loads_object(args)
        except JsonBoundaryError:
            return None
    if not isinstance(args, dict):
        return None

    # Nessun default inventato: se manca un parametro obbligatorio si rinuncia.
    # E' la regola che impedisce il ritorno dei file spuri creati dalla v1.
    if REQUIRED_ARGS.get(name, set()) - set(args):
        return None

    return {
        "id": f"recovered_{int(time.time() * 1000) % 100000}_{seq}",
        "name": name,
        "arguments": json.dumps(args, ensure_ascii=False),
    }


def parse_text_tool_calls(text: str) -> tuple[list[dict[str, Any]], str]:
    """Recupera le tool call che il modello ha stampato come testo.

    Restituisce ``(chiamate, testo_residuo)``: il residuo e' il messaggio
    ripulito dai blocchi JSON consumati, cosi' l'eventuale prosa attorno resta
    visibile in chat mentre la chiamata finisce nella tendina del tool.
    """
    if not text or "{" not in text:
        return [], text

    calls: list[dict[str, Any]] = []
    spans: list[tuple[int, int]] = []
    for seq, (start, end, obj) in enumerate(extract_json_objects(text)):
        call = _as_tool_call(obj, seq)
        if call:
            calls.append(call)
            spans.append((start, end))

    if not calls:
        return [], text

    leftover_parts: list[str] = []
    cursor = 0
    for start, end in spans:
        leftover_parts.append(text[cursor:start])
        cursor = end
    leftover_parts.append(text[cursor:])
    leftover = "".join(leftover_parts)
    # Ripulisce i resti delle recinzioni markdown rimaste orfane.
    leftover = re.sub(r"```(?:json|tool_call|tool_code)?\s*```", "", leftover)
    leftover = re.sub(r"\n{3,}", "\n\n", leftover).strip()

    return calls, leftover


def parse_text_tool_call(text: str) -> dict[str, Any] | None:
    """Compatibilita': la prima tool call recuperata, o ``None``."""
    calls, _ = parse_text_tool_calls(text)
    return calls[0] if calls else None


def is_streaming_tool_json(text: str) -> bool:
    """Il testo *in arrivo* e', molto probabilmente, una tool call in JSON.

    Controllo volutamente grezzo perche' gira ad ogni refresh dello stream: la
    prosa non comincia mai con una graffa o con una recinzione ```json. Serve
    alla UI per sostituire il JSON con un segnaposto gia' mentre scorre,
    invece di riversarlo in chat e toglierlo dopo.
    """
    stripped = (text or "").lstrip()
    if not stripped:
        return False
    if stripped.startswith("```"):
        newline = stripped.find("\n")
        stripped = stripped[newline + 1:].lstrip() if newline != -1 else ""
    return stripped.startswith("{") and '"' in stripped


def looks_like_raw_tool_json(text: str) -> bool:
    """Il testo e' (anche) un blob JSON di tool call non eseguibile?

    Serve alla UI: un output del genere non va mai riversato in chat come
    markdown, nemmeno quando il recupero fallisce.
    """
    if not text or "{" not in text:
        return False
    for _, _, obj in extract_json_objects(text):
        candidate = obj.get("function") if isinstance(obj.get("function"), dict) else obj
        name = candidate.get("name") or candidate.get("tool") or candidate.get("tool_name")
        if isinstance(name, str) and name in TOOL_NAMES:
            return True
    return False


# Segnali che il testo non e' un'azione mancata ma una domanda: il modello si
# e' fermato perche' la richiesta non basta a decidere. Osservato in sessione:
# qwen chiedeva correttamente cosa intendesse l'utente e l'harness gli
# rispondeva "hai risposto a parole, esegui adesso l'azione", spingendolo a
# inventarsi la specifica invece di aspettarla. Il nudge combatteva contro la
# regola del system prompt che gli dice di chiedere.
_QUESTION_MARKERS = (
    "quale preferisci", "come preferisci", "cosa intendi", "che cosa intendi",
    "vuoi che", "preferisci che", "ho bisogno di sapere", "mi serve sapere",
    "prima di procedere ho bisogno", "prima di procedere mi serve",
    "non e' chiaro se", "non è chiaro se", "non ho abbastanza",
    "puoi chiarire", "puoi confermare", "confermi che", "fammi sapere",
    "ci sono due possibili", "due interpretazioni", "ambiguo",
    "which would you", "could you clarify", "do you want me to",
)


def looks_like_clarifying_question(text: str) -> bool:
    """Il modello sta chiedendo un chiarimento, non rimandando un'azione?

    Due indizi indipendenti, perche' uno solo sbaglia troppo: un marcatore
    esplicito fra quelli sopra, oppure una densita' di punti interrogativi che
    in una risposta operativa non ci sarebbe.
    """
    body = strip_think(text or "").strip()
    if not body:
        return False
    lowered = body.lower()
    if any(marker in lowered for marker in _QUESTION_MARKERS):
        return True
    # Almeno due domande, o una sola domanda in una risposta corta: e' una
    # richiesta di chiarimento, non un preambolo prima di agire.
    domande = body.count("?")
    return domande >= 2 or (domande == 1 and len(body) < 400 and body.rstrip().endswith("?"))


def looks_like_unexecuted_action(text: str) -> bool:
    """Euristica: il modello promette un'azione invece di eseguirla."""
    if not text:
        return False
    lowered = strip_think(text).lower()
    if len(lowered) < 15:
        return False
    intents = (
        "creo il file", "creero", "ora scrivo", "adesso scrivo", "procedo a",
        "vado a creare", "vado a modificare", "eseguo il comando", "lancio il comando",
        "ti creo", "scrivero", "modifichero", "posso creare", "dovrei leggere",
        "let me create", "i will create", "i'll write", "i will run",
    )
    return any(token in lowered for token in intents)


# Verbi che, nella richiesta dell'utente, implicano un'operazione sul disco.
_ACTION_VERBS = (
    "crea", "creare", "genera", "generare", "scrivi", "scrivere", "aggiungi",
    "aggiungere", "modifica", "modificare", "correggi", "correggere", "sistema",
    "rinomina", "rifattorizza", "refactor", "implementa", "implementare",
    "cancella", "elimina", "leggi", "leggere", "apri", "mostrami il file",
    "elenca", "lista", "guarda", "ispeziona", "analizza il", "controlla",
    "verifica", "esegui", "eseguire", "lancia", "avvia", "installa", "testa",
    "trova", "cerca", "dove si trova", "dove sta", "quali file", "che file",
    "compila", "committa", "ricordati", "memorizza",
)

# Sostantivi che ancorano la richiesta al workspace.
_WORKSPACE_NOUNS = (
    "file", "cartella", "directory", "progetto", "repo", "repository", "codice",
    "script", "modulo", "funzione", "classe", "test", "workspace", "comando",
    ".py", ".js", ".ts", ".json", ".md", ".txt", ".toml", ".yml", ".yaml",
)


# Radici di verbi che, ripetute, segnalano una richiesta con piu' obiettivi.
# Sono radici e non parole intere per contare "correggi" e "correggere" una
# volta sola: con le forme complete il conteggio si gonfiava da solo e
# qualunque frase lunga sembrava multi-step.
_MULTI_STEP_STEMS = (
    "crea", "genera", "scriv", "aggiung", "modific", "corregg", "implement",
    "rimuov", "elimin", "esegu", "lanci", "verific", "analizz", "document",
    "rifattorizz", "estend", "ottimizz", "riscriv", "sposta", "rinomin",
)

# "STEP 1", "1.", "2)" a inizio riga: la firma tipografica di un elenco di
# compiti. Due occorrenze bastano -- una sola puo' essere un esempio.
_STEP_MARKER = re.compile(r"(?:^|\n)\s*(?:step\s*\d+|\d+[.)])\s", re.I)

# Un file con estensione nota, oppure un percorso di cartella. Le estensioni
# sono un elenco chiuso e non `\w+` perche' altrimenti "3.12" e "art. 81" e
# ogni frase che finisce con un punto diventerebbero file.
_ARTEFATTO = re.compile(
    r"[\w./-]*\w[\w-]*\.(?:py|md|txt|csv|tsv|json|ya?ml|toml|ini|cfg|sql|sh|"
    r"bat|ps1|html?|css|jsx?|tsx?|rs|go|java|c|h|cpp|rb|php|xml|lock)\b"
    r"|[\w-]+(?:/[\w-]+)*/",
)

# I candidati su cui cercare artefatti: sequenze dei caratteri che un percorso
# puo' contenere. Linear-time, e fuori da questi caratteri ``_ARTEFATTO`` non
# puo' comunque trovare niente.
_PAROLA_PERCORSO = re.compile(r"[\w./-]+")
MAX_PAROLA_PERCORSO = 200

# Quanti artefatti distinti fanno una richiesta strutturata. Quattro: sotto,

# "scrivi il modulo e il suo test" ne conta due o tre ed e' un compito solo.
MIN_ARTEFATTI = 4

# Le radici d'azione cercate a inizio parola. Il confronto per sottostringa
# contava "gestendo" come "estend" e "documento" come "document": rumore che
# faceva scattare il sollecito su richieste con un obiettivo solo.
_RADICI_AZIONE = re.compile(
    r"\b(?:" + "|".join(_MULTI_STEP_STEMS) + r")", re.I
)


def artefatti_nominati(text: str) -> set[str]:
    """I file e le cartelle distinti che la richiesta nomina.

    E' il terzo segnale, aggiunto dopo una prova andata male: una richiesta
    scritta in prosa -- "dentro `cantiere/grezzi/` quattro file di testo...
    `cantiere/misura.py` ne ricava `cantiere/misure.csv`..." -- non ha nessun
    elenco numerato e nessun verbo all'imperativo, quindi passava sotto i primi
    due segnali senza toccarli. Ma nominava otto artefatti da produrre, e otto
    artefatti sono otto lavori: il modello e' partito senza piano e ha finito i
    passi. Contare le cose da consegnare coglie la struttura anche quando chi
    scrive descrive un risultato invece di ordinare delle azioni.
    """
    # La regex si applica **parola per parola**, e le parole troppo lunghe per
    # essere un percorso si saltano. Sul testo intero il suo costo era cubico
    # nella lunghezza di una sequenza senza spazi (``[\w./-]*`` e ``\w[\w-]*``
    # si contendono gli stessi caratteri): misurato, 0,5 s a 1.000 caratteri,
    # 31 s a 4.000. Una riga di base64 o di JS minificato incollata nella
    # richiesta bloccava l'avvio del turno -- tenendo il GIL, quindi anche il
    # resto del server. Su una parola di 200 caratteri il caso peggiore e' 5 ms.
    trovati = {
        m.group(0).lower().lstrip("./")
        for parola in _PAROLA_PERCORSO.findall(text or "")
        if len(parola) <= MAX_PAROLA_PERCORSO
        for m in _ARTEFATTO.finditer(parola)
    }
    # `cantiere/` e `cantiere/grezzi/` sono due contenitori diversi, ma
    # `cantiere/` e `cantiere/misura.py` non vanno contati due volte: la
    # cartella che contiene una cosa gia' contata non e' un lavoro in piu'.
    return {
        a for a in trovati
        # Via i contenitori gia' rappresentati da qualcosa che sta dentro
        # (`cantiere/` accanto a `cantiere/misura.py`) e le citazioni in forma
        # breve della stessa cosa (`lunghi/` accanto a `cantiere/lunghi/`):
        # nominare un artefatto due volte non lo raddoppia.
        if not any(b != a and (b.startswith(a) or b.endswith(a)) for b in trovati)
    }


def looks_multi_step(text: str) -> bool:
    """La richiesta contiene piu' obiettivi distinti?

    Serve a decidere se pretendere un piano. Volutamente conservativa: un
    falso positivo costa un giro di manage_plan su un compito semplice
    (fastidioso), un falso negativo riporta al problema di partenza -- il
    modello che prova a fare tutto in un ragionamento solo (grave). La soglia
    sulla lunghezza esiste perche' "leggi il file e dimmi cosa fa" ha due verbi
    ma un obiettivo solo.

    Tre segnali indipendenti, in ordine di quanto sono difficili da sbagliare:
    la tipografia di un elenco, il numero di verbi d'azione, il numero di
    artefatti nominati. Basta uno.
    """
    if not text or len(text) < 220:
        return False
    if len(_STEP_MARKER.findall(text)) >= 2:
        return True
    if len(_RADICI_AZIONE.findall(text)) >= 3:
        return True
    return len(artefatti_nominati(text)) >= MIN_ARTEFATTI


def user_expects_tool_use(text: str) -> bool:
    """La richiesta dell'utente implica un'operazione sul workspace?

    Serve al nudge automatico: se l'utente ha chiesto di *fare* qualcosa e il
    modello ha risposto solo a parole, l'harness lo sollecita da solo invece di
    lasciare che sia l'utente a doverlo fare ogni volta -- che era esattamente
    il sintomo riportato ("i tool li usa solo se glielo dico esplicitamente").
    """
    if not text:
        return False
    lowered = " " + text.lower().strip() + " "
    has_verb = any(v in lowered for v in _ACTION_VERBS)
    has_noun = any(n in lowered for n in _WORKSPACE_NOUNS)
    return has_verb and has_noun


# ---------------------------------------------------------------------------
# Segnali nuovi (25/09/2026): canale sbagliato, chiusure senza prova, ripresa
# ---------------------------------------------------------------------------

_TAG_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)


def nomi_chiamate_nel_testo(text: str) -> list[str]:
    """I tool che il modello ha *scritto* invece di chiamare, in ordine.

    Serve alla rete ``canale``, che non esegue niente: basta riconoscere la
    forma. Per questo e' piu' larga di ``parse_text_tool_calls`` -- che invece
    deve decidere se una chiamata e' eseguibile e rinuncia quando manca un
    argomento -- e guarda anche l'involucro ``<tool_call>`` dei template Qwen,
    che su Ollama senza grammatica finisce a volte nel testo tale e quale.
    """
    if not text or "{" not in text:
        return []
    nomi: list[str] = []
    candidati = [m.group(1) for m in _TAG_TOOL_CALL.finditer(text)]
    oggetti = [obj for _, _, obj in extract_json_objects(text)]
    for grezzo in candidati:
        try:
            oggetti.append(loads_object(grezzo))
        except JsonBoundaryError:
            continue
    for obj in oggetti:
        interno = obj.get("function") if isinstance(obj.get("function"), dict) else obj
        nome = interno.get("name") or interno.get("tool") or interno.get("tool_name")
        if isinstance(nome, str) and nome in TOOL_NAMES and nome not in nomi:
            nomi.append(nome)
    return nomi


# "Fatto", "completato", "ho aggiunto/creato/modificato..." -- la forma in cui
# un modello dichiara chiuso un lavoro. In italiano e in inglese perche' Qwen
# risponde nella lingua della richiesta ma a volte scivola.
_DICHIARAZIONE = re.compile(
    r"\b(?:fatto|completat[oaie]|terminat[oaie]|finit[oaie]|"
    r"ho\s+(?:gia'?\s+)?(?:aggiunto|creato|modificato|corretto|scritto|implementato|"
    r"rimosso|sistemato|aggiornato|rinominato|spostato|eliminato|risolto)|"
    r"(?:e'|è)\s+(?:stato|stata)\s+(?:aggiunt|creat|modificat|corrett|scritt|implementat|"
    r"rimoss|sistemat|aggiornat)[oa]|"
    r"done|completed|i\s+(?:have\s+)?(?:added|created|modified|fixed|updated|implemented))\b",
    re.I,
)

# Verbi che chiedono di cambiare il disco. Radici, come ``_MULTI_STEP_STEMS``.
_RADICI_MODIFICA = re.compile(
    r"\b(?:crea|aggiung|modific|corregg|scriv|implement|rimuov|elimin|sistem|"
    r"rinomin|spost|aggiorn|sostitu|refactor|rifattorizz|fix|add|create|write|"
    r"update|implement|remove|rename)",
    re.I,
)


def dichiara_completamento(text: str) -> bool:
    """La risposta dice che il lavoro e' stato fatto?"""
    corpo = strip_think(text or "").strip()
    return bool(corpo) and bool(_DICHIARAZIONE.search(corpo))


def chiede_modifiche(text: str) -> bool:
    """La richiesta dell'utente chiede di cambiare qualcosa sul disco?"""
    return bool(_RADICI_MODIFICA.search(text or ""))


# "continua", "vai avanti", "prosegui"... e varianti con un complemento corto.
_PROSECUZIONE = re.compile(
    r"^\s*(?:ok[,.!\s]*)?(?:continua|prosegui|vai\s+avanti|avanti|riprendi|procedi|"
    r"finisci|completa|termina|go\s+on|continue|keep\s+going)\b",
    re.I,
)
MAX_CARATTERI_PROSECUZIONE = 80


def e_una_prosecuzione(text: str) -> bool:
    """Il messaggio chiede di continuare il lavoro di prima, non un lavoro nuovo?

    Stretto di proposito: la ripresa riporta nel turno i rossi del turno
    precedente, e un rosso di ieri che insegue una richiesta diversa sarebbe
    rumore. Quindi solo "continua" e affini, e solo se il messaggio e' corto.
    """
    testo = (text or "").strip()
    return bool(testo) and len(testo) <= MAX_CARATTERI_PROSECUZIONE and bool(
        _PROSECUZIONE.match(testo)
    )
