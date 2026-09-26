"""Compattazione selettiva: togliere chiamate vecchie invece di riassumerle.

Da dove viene
-------------
E' l'idea di ``fast-jev-compaction`` (tamaratran, settembre 2026): quando il
contesto si riempie, invece di chiedere a un LLM un riassunto -- lento e con
perdita: un percorso, un errore esatto, un vincolo possono sparire -- si
chiede a un **modello di decisione** (Jev di TypeSafe), per ogni chiamata di
tool vecchia, due probabilita': la chiamata serve ancora? il suo risultato
serve ancora parola per parola? Sotto soglia il risultato si accorcia o la
coppia chiamata+risultato sparisce. Il testo dell'utente e dell'assistente non
si tocca mai.

Qui il valutatore e' **intercambiabile** e parla il protocollo ``/v1/systemone``
di Jev, che e' anche quello di ``laya-serve`` (Laya, ConvAI Innovations,
Apache 2.0): lo stesso client funziona con Laya in locale, senza chiave e
senza mandare la conversazione fuori dalla macchina.

Cosa cambia rispetto all'originale, e perche'
---------------------------------------------
1. **Stato per chiamata, non la conversazione intera.** fast-jev manda a Jev
   tutta la cronologia (fino a 25k token, sotto il limite di 32k di Jev) e
   chiede di ogni chiamata. Laya legge 512 token (inglese), 1024 (multilingue,
   fino a 8192 con accuratezza che cala oltre ~4000) e **tronca in silenzio**
   uno stato-stringa piu' lungo tenendone l'inizio: puntato sulla conversazione
   intera risponderebbe su un pezzo che non contiene la chiamata chiesta.
   Qui ogni chiamata ha il suo stato breve (obiettivo, chiamata, inizio
   dell'esito, fatti successivi), sotto ``MAX_CARATTERI_STATO``.
2. **I fatti successivi li calcola l'harness.** Uno stato locale non vede il
   futuro della conversazione; e il segnale piu' forte ("questo file e' stato
   riletto o riscritto dopo", "questo comando e' stato rilanciato") e' un
   fatto certo, non una stima. Le regole lo mettono nello stato; il
   valutatore decide il resto.
3. **Senza valutatore funziona lo stesso.** ``probabilita_regole`` decide con
   le sole regole. E' il default, ed e' anche il termine di paragone: un modello
   di decisione deve battere le regole, non il nulla.
4. **La vista, non la cronologia.** Come il riassunto, la selezione non
   cancella niente: e' un record ``selezione`` in ``ui_messages`` con le
   decisioni per id di chiamata, applicato da ``build_api_messages``. Le
   decisioni sono fisse dopo lo scatto, quindi il prefisso resta stabile.
5. **Se non basta, riassunto.** Come in fast-jev (``minReductionRatio``): se
   dopo la selezione il contesto resta sopra ``QUOTA_OBIETTIVO`` della
   finestra, si scarta e si riassume come prima.

In questo harness i risultati vecchi sono gia' compattati a ogni passo
(``risultati_integrali``, isteresi del 22/09), cioe' fast-jev "drop_result" e'
gia' la norma: la leva nuova e' soprattutto ``elimina`` (la coppia intera) e
il riassunto evitato.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

RUOLO = "selezione"

# Stesse manopole di fast-jev-compaction, con i suoi valori.
SOGLIA_TENUTA = 0.5          # keepThreshold
TESTA_TRONCATA = 300         # truncateHeadChars
# Dopo la selezione il contesto deve stare sotto questa quota della finestra
# efficace, o si ripiega sul riassunto. La compattazione scatta a 0,75: a 0,55
# restano ~6.500 token di margine su 32k, cioe' qualche passo prima di
# riscattare. Sopra, la selezione riscatterebbe quasi a ogni passo.
QUOTA_OBIETTIVO = 0.55
# Tempo concesso al valutatore per una compattazione. Oltre, le chiamate non
# ancora valutate si decidono con le regole: il turno non aspetta un modello
# di decisione lento (Laya su CPU: ~0,2-0,6 s a domanda).
TEMPO_MASSIMO_S = 20.0
# ~700 token: con le due domande sta nei 1024 di laya-multilingual.
MAX_CARATTERI_STATO = 2_400
MAX_OBIETTIVO = 600
MAX_ARGOMENTI = 300
MAX_TESTA_ESITO = 500

# Mai candidati: una risposta umana e' specifica, non un log.
PROTETTI = frozenset({"ask_user_question"})
LETTURE = frozenset({"read_file"})
SCRITTURE = frozenset({"write_file", "edit_file"})
RICERCHE = frozenset({"search_files", "list_files", "grep_files", "find_files"})

CONTESTO_STATO = (
    "A coding agent's conversation is being compacted to free context. `call` is "
    "one old tool call, `result_head` the start of its output, `later` what the "
    "harness verified happened after it, `goal` the user's current request. "
    "Whatever is not kept is removed from the agent's view; the agent can re-run "
    "a tool or re-read a file."
)


# ---------------------------------------------------------------------------
# Candidati e fatti successivi
# ---------------------------------------------------------------------------


@dataclass
class Candidato:
    """Una chiamata vecchia con il suo risultato, candidata a sparire."""

    id: str
    nome: str
    args: dict[str, Any]
    pos_chiamata: int
    pos_esito: int
    ok: bool
    esito: str
    fatti: list[str] = field(default_factory=list)
    superata: bool = False
    # L'oggetto (file, comando, pattern) e' stato nominato di nuovo dopo il
    # risultato -- nel testo dell'assistente, in un'altra chiamata, in una
    # richiesta -- fino ad adesso? E' il segnale osservabile che il risultato
    # e' in uso; il suo contrario, "letto e mai piu' ripreso", e' il vicolo
    # cieco di un'esplorazione. None: oggetto troppo generico per dirlo.
    ripreso: bool | None = None

    @property
    def bersaglio(self) -> str:
        a = self.args
        return str(a.get("filepath") or a.get("command") or a.get("pattern")
                   or a.get("path") or a.get("query") or "")


def _args(call: dict[str, Any]) -> dict[str, Any]:
    fn = call.get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (TypeError, ValueError):
        return {}
    return args if isinstance(args, dict) else {}


def _esito_fallito(msg: dict[str, Any]) -> bool:
    if msg.get("ok") is False:
        return True
    testo = str(msg.get("content") or "")
    return '"esito": "FALLITO"' in testo or testo.lstrip().startswith('{"error"')


def decisioni_attive(ui_messages: Sequence[dict[str, Any]]) -> dict[str, str]:
    """Le decisioni in vigore: quelle dei record dopo l'ultimo riassunto."""
    inizio = 0
    for i, m in enumerate(ui_messages):
        if m.get("role") == "summary":
            inizio = i
    decisioni: dict[str, str] = {}
    for m in ui_messages[inizio:]:
        if m.get("role") == RUOLO:
            for k, v in (m.get("decisioni") or {}).items():
                if v in ("tronca", "elimina"):
                    decisioni[str(k)] = v
    return decisioni


def candidati(ui_messages: Sequence[dict[str, Any]], inizio: int, fine: int) -> list[Candidato]:
    """Le chiamate in ``[inizio, fine)`` con un risultato, non protette, non gia' tolte."""
    esiti: dict[str, tuple[int, dict[str, Any]]] = {}
    for i, m in enumerate(ui_messages):
        if m.get("role") == "tool":
            esiti[str(m.get("tool_call_id") or "")] = (i, m)
    gia = decisioni_attive(ui_messages)
    fuori: list[Candidato] = []
    for pos in range(inizio, min(fine, len(ui_messages))):
        msg = ui_messages[pos]
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            cid = str(call.get("id") or "")
            nome = str((call.get("function") or {}).get("name") or "")
            if not cid or nome in PROTETTI or cid not in esiti or gia.get(cid) == "elimina":
                continue
            pos_e, m = esiti[cid]
            if m.get("riparato") or m.get("answered"):
                continue
            fuori.append(Candidato(
                id=cid, nome=nome, args=_args(call), pos_chiamata=pos, pos_esito=pos_e,
                ok=not _esito_fallito(m), esito=str(m.get("content") or ""),
            ))
    return fuori


def _norm(testo: str) -> str:
    return " ".join(str(testo).split())


def annota_fatti(ui_messages: Sequence[dict[str, Any]], cand: list[Candidato]) -> None:
    """I fatti che l'harness sa per certo su cosa e' successo *dopo* ogni candidato.

    Non stime: un file riletto per intero, riscritto, un comando rilanciato,
    un errore seguito dalla stessa chiamata riuscita. ``superata`` vuol dire
    che il risultato non descrive piu' lo stato attuale.
    """
    eventi: list[tuple[int, str, dict[str, Any], bool]] = []
    esiti = {str(m.get("tool_call_id") or ""): m for m in ui_messages if m.get("role") == "tool"}
    for i, m in enumerate(ui_messages):
        if m.get("role") != "assistant":
            continue
        for call in m.get("tool_calls") or []:
            cid = str(call.get("id") or "")
            nome = str((call.get("function") or {}).get("name") or "")
            risultato = esiti.get(cid)
            if risultato is None:
                continue
            eventi.append((i, nome, _args(call), not _esito_fallito(risultato)))
    for c in cand:
        dopo = [(i, n, a, ok) for i, n, a, ok in eventi if i > c.pos_chiamata]
        fp = str(c.args.get("filepath") or "")
        if c.nome in LETTURE and fp:
            parziale = bool(c.args.get("start_line") or c.args.get("end_line"))
            if any(n in LETTURE and ok and str(a.get("filepath") or "") == fp
                   and (parziale or not (a.get("start_line") or a.get("end_line")))
                   for _, n, a, ok in dopo):
                c.fatti.append("the same file was read again later")
                c.superata = True
            if any(n in SCRITTURE and ok and str(a.get("filepath") or "") == fp
                   for _, n, a, ok in dopo):
                c.fatti.append("the file was modified later: this content is outdated")
                c.superata = True
        elif c.nome in SCRITTURE and fp:
            if any(n in SCRITTURE and ok and str(a.get("filepath") or "") == fp
                   for _, n, a, ok in dopo):
                c.fatti.append("the same file was modified again later")
                c.superata = True
        elif c.nome == "run_command":
            cmd = _norm(c.args.get("command") or "")
            rilanci = [ok for _, n, a, ok in dopo
                       if n == "run_command" and _norm(a.get("command") or "") == cmd]
            if rilanci:
                c.fatti.append(
                    "the same command was run again later"
                    + (" and succeeded" if rilanci[-1] else " and failed again")
                )
                c.superata = True
        elif c.nome in RICERCHE:
            chiave = json.dumps(c.args, sort_keys=True)
            if any(n == c.nome and json.dumps(a, sort_keys=True) == chiave for _, n, a, _ok in dopo):
                c.fatti.append("the same search was repeated later")
                c.superata = True
        if not c.ok and not c.superata and any(
            n == c.nome and ok and (str(a.get("filepath") or a.get("command") or "")
                                    == c.bersaglio)
            for _, n, a, ok in dopo
        ):
            c.fatti.append("this failed, and the same call succeeded later")
            c.superata = True
        c.ripreso = _ripreso_dopo(ui_messages, c)
        if c.ripreso is True:
            c.fatti.append("its subject was mentioned again later")
        elif c.ripreso is False:
            c.fatti.append("its subject was never mentioned again")


def _oggetto(c: Candidato) -> str:
    """Cosa cercare dopo: il nome del file, il comando, il pattern."""
    grezzo = _norm(c.bersaglio)
    if not grezzo:
        return ""
    if c.nome in LETTURE or c.nome in SCRITTURE:
        base = grezzo.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return base if len(base) >= 4 else grezzo
    return grezzo[:60] if len(grezzo) >= 4 else ""


def _ripreso_dopo(ui_messages: Sequence[dict[str, Any]], c: Candidato) -> bool | None:
    oggetto = _oggetto(c)
    if not oggetto:
        return None
    for m in ui_messages[c.pos_esito + 1:]:
        ruolo = m.get("role")
        if ruolo == "assistant":
            if oggetto in _norm(m.get("content") or ""):
                return True
            for call in m.get("tool_calls") or []:
                if oggetto in _norm((call.get("function") or {}).get("arguments") or ""):
                    return True
        elif ruolo == "user" and not m.get("hidden") and oggetto in _norm(m.get("content") or ""):
            return True
    return False


# ---------------------------------------------------------------------------
# Lo stato e le domande
# ---------------------------------------------------------------------------


def _breve(testo: str, n: int) -> str:
    testo = str(testo)
    return testo if len(testo) <= n else testo[: n - 1] + "…"


def stato_locale(c: Candidato, obiettivo: str, piano: str = "") -> dict[str, Any]:
    """Lo stato di una chiamata, dentro la finestra di Laya."""
    args = {k: (_breve(v, 160) if isinstance(v, str) else v) for k, v in c.args.items()}
    stato: dict[str, Any] = {
        "context": CONTESTO_STATO,
        "goal": _breve(obiettivo, MAX_OBIETTIVO),
        "call": {"tool": c.nome, "input": _breve(json.dumps(args, ensure_ascii=False),
                                                  MAX_ARGOMENTI)},
        "result_head": _breve(c.esito, MAX_TESTA_ESITO),
        "result_chars": len(c.esito),
        "result_ok": c.ok,
        "later": c.fatti or ["nothing related happened later"],
    }
    if piano:
        stato["open_plan"] = _breve(piano, 300)
    # Garanzia sulla misura: si accorcia l'esito, poi l'obiettivo.
    while len(json.dumps(stato, ensure_ascii=False)) > MAX_CARATTERI_STATO:
        if len(stato["result_head"]) > 120:
            stato["result_head"] = _breve(stato["result_head"], len(stato["result_head"]) // 2)
        elif len(stato["goal"]) > 150:
            stato["goal"] = _breve(stato["goal"], len(stato["goal"]) // 2)
        else:
            break
    return stato


def domande(c: Candidato) -> dict[str, dict[str, Any]]:
    """Le due domande di fast-jev-compaction, con criteri espliciti."""
    return {
        "keep_call": {
            "type": "noul",
            "instructions": (
                f"The {c.nome} call should stay in the agent's history: knowing that it "
                "was made, with its input, still matters for what the agent does next."
            ),
            "criteria": {
                "true": "the call is part of the work still in progress or explains a decision",
                "false": "the call is routine, repeated, or about something already finished",
            },
        },
        "keep_result": {
            "type": "noul",
            "instructions": (
                f"The full output of this {c.nome} call ({len(c.esito)} characters) should "
                "stay verbatim: the agent still needs its contents and re-running the tool "
                "would not do."
            ),
            "criteria": {
                "true": "the output is current and needed for the goal, and cannot be re-obtained",
                "false": "the output is outdated, superseded, or can be re-obtained by re-running",
            },
        },
    }


# ---------------------------------------------------------------------------
# Valutatori
# ---------------------------------------------------------------------------


class Valutatore(Protocol):
    nome: str

    def valuta(self, stato: dict[str, Any], domande: dict[str, dict[str, Any]]) -> dict[str, float]:
        """Probabilita' ``noul`` per nome di domanda. Solleva se non risponde."""
        ...


def probabilita_regole(c: Candidato, obiettivo: str) -> tuple[float, float]:
    """(tieni la chiamata, tieni il risultato) secondo le sole regole.

    Non sono stime tarate: sono una politica scritta in forma di probabilita',
    cosi' che regole e valutatore passino dalla stessa ``decidi``.
    """
    bersaglio = _norm(c.bersaglio)[:80]
    base = bersaglio.replace("\\", "/").rsplit("/", 1)[-1]
    citato = bool(bersaglio) and (bersaglio in obiettivo or (len(base) > 3 and base in obiettivo))
    if c.superata:
        return (0.6 if c.nome in SCRITTURE or c.nome == "run_command" else 0.2), 0.1
    if not c.ok:
        return 0.8, 0.6            # l'errore attuale: resta
    # "Letto e mai piu' nominato" NON basta per togliere. Provato il 26/09
    # come regola (via tutto cio' che non e' stato ripreso): sul replay delle
    # 54 sessioni evitava 3-6 riassunti in piu', ma il 30-48% di cio' che
    # toglieva la sessione vera lo richiedeva di nuovo dopo, contro il 24-26%
    # delle chiamate tolte dal riassunto. Il fatto resta nello stato che vede
    # il valutatore; la decisione no.
    if c.nome in LETTURE:
        return 0.7, (0.7 if citato or c.ripreso else 0.4)
    if c.nome == "run_command":
        return 0.7, 0.3
    if c.nome in SCRITTURE:
        return 0.8, 0.3
    if c.nome in RICERCHE:
        return 0.55, 0.35
    if c.nome == "manage_plan":
        return 0.2, 0.1            # il piano vive nel blocco di coda
    if c.nome in ("esplora", "web_search", "vault_search"):
        return 0.7, 0.6            # referti e fonti esterne: costano da rifare
    return 0.6, 0.5


def _host_typesafe(url: str) -> bool:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host == "typesafe.ai" or host.endswith(".typesafe.ai")


class ValutatoreSystemOne:
    """Client del protocollo ``/v1/systemone``: Laya in locale, o Jev.

    ``laya-serve`` espone lo stesso protocollo di Jev (``laya/serve.py``):
    ``{"model", "state", "questions"}`` in, ``{"answers": {nome: {"noul": p}}}``
    fuori. ``model`` accetta i nomi dei checkpoint di Laya: "multilingual" per
    una conversazione in italiano (il router, lasciato fare, manderebbe il
    codice inglese al checkpoint inglese da 512 token).
    """

    def __init__(self, url: str, modello: str = "multilingual", *, chiave: str = "",
                 timeout_s: float = 10.0, client: Any = None) -> None:
        self.url = url
        self.modello = modello
        # Mai nelle impostazioni: solo dall'ambiente, e solo verso l'host di
        # TypeSafe -- per nome di host, non per sottostringa dell'indirizzo
        # ("http://altro.host/typesafe.ai" non deve ricevere la chiave).
        e_jev = _host_typesafe(url)
        self.chiave = chiave or (os.environ.get("TYPESAFE_API_KEY", "") if e_jev else "")
        self.timeout_s = timeout_s
        self._client = client
        self.nome = "jev" if e_jev else "laya"

    def valuta(self, stato: dict[str, Any], domande: dict[str, dict[str, Any]]) -> dict[str, float]:
        import httpx

        intestazioni = {"content-type": "application/json"}
        if self.chiave:
            intestazioni["authorization"] = f"Bearer {self.chiave}"
        corpo = {"model": self.modello, "state": stato, "questions": domande}
        client = self._client or httpx
        risposta = client.post(self.url, json=corpo, headers=intestazioni,
                               timeout=self.timeout_s)
        if risposta.status_code != 200:
            raise RuntimeError(f"system one: HTTP {risposta.status_code}")
        dati = risposta.json()
        risposte = dati.get("answers") if isinstance(dati, dict) else None
        if not isinstance(risposte, dict):
            raise RuntimeError("system one: risposta senza 'answers'")
        fuori: dict[str, float] = {}
        for nome in domande:
            voce = risposte.get(nome)
            p = voce.get("noul") if isinstance(voce, dict) else None
            if not isinstance(p, (int, float)) or not 0.0 <= float(p) <= 1.0:
                raise RuntimeError(f"system one: risposta non valida per {nome}")
            fuori[nome] = float(p)
        return fuori


# ---------------------------------------------------------------------------
# Decisione e selezione
# ---------------------------------------------------------------------------


def decidi(p_chiamata: float, p_esito: float, soglia: float = SOGLIA_TENUTA) -> str:
    """La regola di fast-jev-compaction (``decideCall``)."""
    if p_esito >= soglia:
        return "tieni"
    if p_chiamata >= soglia:
        return "tronca"
    return "elimina"


@dataclass
class Selezione:
    record: dict[str, Any]
    candidati: int
    tenuti: int
    troncati: int
    eliminati: int
    domande: int
    dal_valutatore: int
    ms: int
    valutatore: str
    errore: str = ""


def seleziona(
    ui_messages: Sequence[dict[str, Any]],
    inizio: int,
    fine: int,
    *,
    obiettivo: str,
    piano: str = "",
    valutatore: Valutatore | None = None,
    tempo_massimo_s: float = TEMPO_MASSIMO_S,
    orologio: Callable[[], float] = time.monotonic,
) -> Selezione:
    """Decide ogni candidato in ``[inizio, fine)``; non modifica ``ui_messages``."""
    partenza = orologio()
    cand = candidati(ui_messages, inizio, fine)
    annota_fatti(ui_messages, cand)
    decisioni: dict[str, str] = {}
    conteggi = {"tieni": 0, "tronca": 0, "elimina": 0}
    dal_valutatore = 0
    n_domande = 0
    errore = ""
    for c in cand:
        p_chiamata, p_esito = probabilita_regole(c, obiettivo)
        if valutatore is not None and not errore and orologio() - partenza < tempo_massimo_s:
            q = domande(c)
            try:
                risposte = valutatore.valuta(stato_locale(c, obiettivo, piano), q)
                p_chiamata, p_esito = risposte["keep_call"], risposte["keep_result"]
                dal_valutatore += 1
                n_domande += len(q)
            except Exception as exc:  # noqa: BLE001 - un valutatore giu' non ferma il turno
                errore = str(exc)[:200]
        azione = decidi(p_chiamata, p_esito)
        conteggi[azione] += 1
        if azione != "tieni":
            decisioni[c.id] = azione
    ms = int((orologio() - partenza) * 1000)
    nome = getattr(valutatore, "nome", "regole") if valutatore is not None else "regole"
    record = {
        "role": RUOLO,
        "content": "",
        "decisioni": decisioni,
        "valutatore": nome,
        "candidati": len(cand),
        "tenuti": conteggi["tieni"],
        "troncati": conteggi["tronca"],
        "eliminati": conteggi["elimina"],
        "domande": n_domande,
        "dal_valutatore": dal_valutatore,
        "ms": ms,
        "source_start": inizio,
        "source_end": fine,
        "ts": time.time(),
    }
    if errore:
        record["errore_valutatore"] = errore
    return Selezione(record=record, candidati=len(cand), tenuti=conteggi["tieni"],
                     troncati=conteggi["tronca"], eliminati=conteggi["elimina"],
                     domande=n_domande, dal_valutatore=dal_valutatore, ms=ms,
                     valutatore=nome, errore=errore)


def record_tutto_via(ui_messages: Sequence[dict[str, Any]], inizio: int, fine: int) -> dict[str, Any]:
    """Il tetto della selezione: ogni candidato tolto. Serve solo a misurare."""
    decisioni = {c.id: "elimina" for c in candidati(ui_messages, inizio, fine)}
    return {"role": RUOLO, "content": "", "decisioni": decisioni, "valutatore": "tetto"}


def testo_troncato(testo: str, testa: int = TESTA_TRONCATA) -> str:
    """Il risultato accorciato, con la nota che dice come riaverlo."""
    if len(testo) <= testa + 120:
        return testo
    return (
        f"{testo[:testa]}\n[compattazione selettiva: tolti {len(testo) - testa} caratteri "
        "di questo risultato; rilancia il tool se ti serve]"
    )


def descrivi(sel: Selezione) -> str:
    """Una riga per l'evento e per la cronologia della UI."""
    fonte = sel.valutatore if sel.dal_valutatore else "regole"
    riga = (
        f"Compattazione selettiva ({fonte}): {sel.candidati} chiamate vecchie, "
        f"{sel.tenuti} tenute, {sel.troncati} accorciate, {sel.eliminati} tolte "
        f"dalla vista del modello; nessun riassunto."
    )
    if sel.errore:
        riga += f" Valutatore non disponibile ({sel.errore}): decise le regole."
    return riga
