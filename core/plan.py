"""Piano di lavoro del turno: l'agente lo scrive, l'harness lo fa rispettare.

Il problema
-----------
Davanti a una richiesta lunga -- il caso reale era un prompt da 7.000 caratteri
con cinque step numerati -- il modello prova a risolvere **tutto in testa prima
di toccare il disco**. Su qwen3.8 questo ha prodotto 8.200 token di ragionamento
in cui pianificava tutti e cinque gli step, scriveva mentalmente il motore e
ragionava sulla modalita' strict di pytest-asyncio, per poi sbattere contro il
tetto di ``num_predict`` e chiudere il turno **senza aver emesso una sola tool
call**. Due volte di fila, a 0,4% di distanza in lunghezza: non un caso, un
tetto.

Perche' un piano risolve, e non e' burocrazia
---------------------------------------------
Il ragionamento e' l'unico posto dove il modello puo' tenere lo stato del
lavoro, e il ragionamento **viene buttato via ad ogni turno**
(``strip_think_from_context``). Quindi ad ogni passo deve ricostruirlo da capo,
e piu' il compito e' lungo piu' quella ricostruzione costa -- fino a costare
tutto il budget. Un piano scritto e' lo stesso stato, ma fuori dal ragionamento:
sopravvive ai turni, costa cinquanta token invece di ottomila, e soprattutto
**e' leggibile anche da chi guarda**.

Coerente col principio del progetto: le abitudini non si ottengono col prompt,
si ottengono dando l'informazione in contesto o rendendo il comportamento
sbagliato meccanicamente impossibile. Qui si fanno tutte e due:

* il piano viene iniettato **in coda** al contesto ad ogni passo -- il modello
  non deve ricordarselo, ce l'ha davanti (vedi ``render_block`` per il motivo
  per cui va in coda e non in testa);
* un solo punto puo' essere ``in corso`` per volta. Aprirne un secondo senza
  aver chiuso il primo viene **rifiutato**: e' esattamente la mossa di chi sta
  cercando di fare tutto insieme.

Questo modulo non sa niente ne' dei tool ne' della UI: e' una struttura dati
con le sue regole. Le guardie che dipendono dallo stato del turno -- per esempio
"non puoi dichiarare fatto un punto mentre una verifica e' rossa" -- stanno in
``tools.py``, che quello stato ce l'ha.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

TODO = "todo"
DOING = "doing"
DONE = "done"
SKIPPED = "skipped"

STATUSES = (TODO, DOING, DONE, SKIPPED)

# Etichette per il blocco di contesto. In italiano perche' le legge il modello
# nello stesso testo in cui gli parliamo italiano; la UI usa le sue.
_LABELS = {
    TODO: "da fare",
    DOING: "in corso",
    DONE: "fatto",
    SKIPPED: "saltato",
}

# Un piano piu' lungo di cosi' non e' un piano, e' il compito riscritto: costa
# contesto ad ogni passo e nessuno lo rilegge. Il taglio e' voluto e dichiarato.
#
# Da v2.27 il tetto vale sui punti **aperti**, non su tutti. E' li' che stava
# davvero il motivo del limite: dodici cose da fare insieme sono ingestibili,
# dodici cose gia' fatte sono solo un registro. Contarli insieme costringeva a
# cancellare la storia per poter pianificare ancora -- ed e' esattamente il
# comportamento che si vuole vietare.
MAX_STEPS = 12
# Quanti punti chiusi si conservano. Non e' un permesso a cancellare la storia:
# e' il tetto oltre il quale una conversazione lunghissima smetterebbe di
# entrare in un file di sessione ragionevole. A 24 non ci e' mai arrivata
# nessuna sessione misurata (la piu' lunga, 19/08/2026, ne ha chiusi 23).
MAX_CLOSED_KEPT = 24
# Quanti punti chiusi finiscono nel blocco che il modello legge ad ogni passo.
# La storia serve a lui per non rifare le cose, ma ricordargli venti punti
# chiusi ad ogni passo costa contesto tutti i passi: gliene bastano gli ultimi,
# piu' una riga che dice quanti ce n'erano prima.
CLOSED_IN_BLOCK = 4
MAX_TEXT_CHARS = 160

# Numerazione che il modello si porta dietro dalla richiesta: "1. ", "3) ",
# "- ", "Punto 2: ". La toglie l'harness perche' il numero di un punto lo
# decide il piano, non chi lo scrive: quello del modello e' quello dell'elenco
# dell'utente, e i due divergono al primo raggruppamento -- con il risultato
# che nel pannello si legge "2." accanto a un punto che per il piano e' il 5.
_NUMERAZIONE = re.compile(r"^\s*(?:punto\s*)?\d{1,2}\s*[.):\-]\s+|^\s*[-*\u2022]\s+", re.I)


def pulisci_testo(text: str) -> str:
    """Il testo di un punto, senza la numerazione che il modello ci mette."""
    testo = str(text).strip()
    # Una volta sola: "1. 2. qualcosa" e' gia' un errore del modello, e
    # spogliarlo fino all'osso nasconderebbe che l'ha scritto cosi'.
    testo = _NUMERAZIONE.sub("", testo, count=1).strip()
    return testo[:MAX_TEXT_CHARS]
MAX_NOTE_CHARS = 200


@dataclass(slots=True)
class PlanStep:
    id: str
    text: str
    status: str = TODO
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "status": self.status, "note": self.note}


class PlanError(ValueError):
    """Mossa rifiutata: il messaggio torna al modello come errore del tool."""


@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)

    # --- serializzazione ---------------------------------------------------

    def to_list(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.steps]

    @classmethod
    def from_list(cls, data: Any) -> Plan:
        """Ricostruisce da disco, scartando in silenzio quello che non torna.

        Un file di sessione scritto da una versione precedente non ha il piano,
        e uno modificato a mano puo' avere qualunque cosa: non e' un motivo per
        non far aprire la conversazione.
        """
        steps: list[PlanStep] = []
        if isinstance(data, list):
            for raw in data:
                if not isinstance(raw, dict):
                    continue
                # La pulizia va fatta anche qui, non solo in scrittura: i
                # piani gia' su disco sono stati salvati con la numerazione
                # del modello dentro, e senza questo passaggio una sessione
                # riaperta mostra due numeri per punto -- il suo e il nostro.
                text = pulisci_testo(str(raw.get("text") or ""))
                if not text:
                    continue
                status = str(raw.get("status") or TODO)
                steps.append(
                    PlanStep(
                        id=str(raw.get("id") or len(steps) + 1),
                        text=text,
                        status=status if status in STATUSES else TODO,
                        note=str(raw.get("note") or "")[:MAX_NOTE_CHARS],
                    )
                )
        return cls(steps=steps)

    # --- interrogazione ----------------------------------------------------

    def __bool__(self) -> bool:
        return bool(self.steps)

    def get(self, step_id: str) -> PlanStep | None:
        wanted = str(step_id).strip()
        for step in self.steps:
            if step.id == wanted:
                return step
        return None

    @property
    def current(self) -> PlanStep | None:
        for step in self.steps:
            if step.status == DOING:
                return step
        return None

    @property
    def open_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status in (TODO, DOING)]

    @property
    def closed_steps(self) -> list[PlanStep]:
        """I punti chiusi, in ordine. Sono la storia del lavoro, non scarto."""
        return [s for s in self.steps if s.status in (DONE, SKIPPED)]

    def _new_id(self) -> str:
        """Il prossimo numero libero, mai uno gia' usato in questa sessione.

        Prima l'id era ``len(self.steps) + 1``, cioe' una posizione travestita
        da identita': bastava che il piano cambiasse lunghezza perche' due
        punti diversi si ritrovassero lo stesso numero -- e il numero e' quello
        che l'utente legge nel pannello e che il modello passa a manage_plan.
        Con i punti chiusi che restano, quella collisione sarebbe la norma.
        """
        usati = [int(s.id) for s in self.steps if s.id.isdigit()]
        return str(max(usati, default=0) + 1)

    @property
    def finished(self) -> bool:
        return bool(self.steps) and not self.open_steps

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(STATUSES, 0)
        for step in self.steps:
            out[step.status] += 1
        return out

    # --- mutazioni ---------------------------------------------------------

    def set_steps(self, texts: list[str]) -> None:
        """Riscrive la **parte aperta** del piano. I punti chiusi non si toccano.

        Rivedere il piano a meta' lavoro e' legittimo: scoprire una dipendenza
        mancante o un caso limite e' proprio quello che succede leggendo il
        codice. Cancellare quello che si e' gia' fatto, no.

        Prima ``set`` sostituiva l'elenco intero e salvava lo stato solo dei
        punti col testo **identico**. Su una conversazione vera quella clausola
        non salva quasi niente: ad ogni nuova richiesta dell'utente il modello
        riscrive il piano con parole nuove, e i punti chiusi sparivano tutti
        insieme. Misurato sulla sessione del 19/08/2026: cinque ``set`` di
        fila, ventitre punti chiusi lungo la strada, **sei** rimasti nel file
        alla fine. Il pannello raccontava un quinto del lavoro svolto, e il
        modello -- che quel blocco lo rilegge ad ogni passo -- non aveva piu'
        modo di sapere cosa avesse gia' finito.

        Adesso: i chiusi restano dove sono, in testa e con i loro numeri; il
        nuovo elenco descrive **solo** cio' che resta da fare, e i suoi punti si
        accodano. Un testo che coincide con un punto gia' chiuso non lo riapre e
        non lo duplica: quel lavoro e' fatto.
        """
        puliti = [p for p in (pulisci_testo(t) for t in texts) if p]
        if not puliti:
            raise PlanError("Il piano non puo' essere vuoto: passa almeno un punto.")

        chiusi = self.closed_steps
        gia_chiusi = {s.text for s in chiusi}
        aperti_per_testo = {s.text: s for s in self.open_steps}

        nuovi_aperti: list[PlanStep] = []
        visti: set[str] = set()
        for testo in puliti:
            if testo in gia_chiusi or testo in visti:
                continue
            visti.add(testo)
            vecchio = aperti_per_testo.get(testo)
            # Riusare l'oggetto e non ricrearlo: cosi' il punto in corso resta
            # in corso e conserva il suo numero, che e' quello che l'utente ha
            # sott'occhio nel pannello mentre l'agente ci sta lavorando.
            nuovi_aperti.append(vecchio if vecchio is not None else PlanStep(id="", text=testo))

        if len(nuovi_aperti) > MAX_STEPS:
            raise PlanError(
                f"Troppi punti aperti ({len(nuovi_aperti)}): il massimo e' "
                f"{MAX_STEPS}. Raggruppa quelli minuti, il piano serve a "
                "orientarsi non a elencare ogni comando."
            )

        # Il taglio della storia cade sui piu' vecchi, non sui piu' recenti: se
        # qualcosa deve uscire, esce quello che serve meno a capire dove siamo.
        self.steps = chiusi[-MAX_CLOSED_KEPT:] + nuovi_aperti
        for step in nuovi_aperti:
            if not step.id:
                step.id = self._new_id()

    def add(self, text: str) -> PlanStep:
        testo = pulisci_testo(text)
        if not testo:
            raise PlanError("Serve il testo del punto da aggiungere.")
        if len(self.open_steps) >= MAX_STEPS:
            raise PlanError(
                f"Il piano ha gia' {MAX_STEPS} punti aperti: chiudine qualcuno."
            )
        step = PlanStep(id=self._new_id(), text=testo)
        self.steps.append(step)
        return step

    def start(self, step_id: str) -> PlanStep:
        """Apre un punto. **Uno solo per volta.**

        E' la guardia centrale del modulo. Il comportamento che si vuole
        impedire non e' "sbagliare punto": e' aprire il secondo, il terzo e il
        quarto senza averne chiuso nessuno, cioe' tornare a fare tutto insieme
        con un piano addosso come travestimento.
        """
        step = self._require(step_id)
        aperto = self.current
        if aperto is not None and aperto.id != step.id:
            raise PlanError(
                f"Il punto {aperto.id} ('{aperto.text}') e' ancora in corso. "
                "Chiudilo con action='complete' (o 'skip' se hai deciso di non "
                "farlo) prima di aprire il punto "
                f"{step.id}. Un punto per volta: e' il modo di non perdersi."
            )
        if step.status in (DONE, SKIPPED):
            raise PlanError(
                f"Il punto {step.id} risulta gia' '{_LABELS[step.status]}'. "
                "Se devi riaprirlo, riscrivi il piano con action='set'."
            )
        step.status = DOING
        return step

    def complete(self, step_id: str, note: str = "") -> PlanStep:
        step = self._require(step_id)
        step.status = DONE
        step.note = str(note or "").strip()[:MAX_NOTE_CHARS]
        return step

    def skip(self, step_id: str, note: str = "") -> PlanStep:
        step = self._require(step_id)
        step.status = SKIPPED
        step.note = str(note or "").strip()[:MAX_NOTE_CHARS]
        return step

    def avanza(self) -> PlanStep | None:
        """Apre il prossimo punto rimasto, se non ce n'e' gia' uno in corso.

        Su una sessione misurata, ``start`` e ``complete`` sono finiti in passi
        tutti loro: tredici passi su trentuno senza una riga di lavoro dentro,
        con la sequenza start / scrivi / esegui / complete ripetuta per sei
        punti. Meta' di quella spesa era per chiedere al modello un'informazione
        che l'harness aveva gia': qual e' il punto dopo. Ora lo apre da se'.

        Non e' una scorciatoia sulla regola "un punto per volta" -- quella
        resta, e ``start`` continua a rifiutare un secondo punto aperto. E'
        l'automatismo che rende quella regola gratuita invece che costosa: se
        il modello vuole andare a un punto diverso da quello in fila, chiama
        ``start`` e paga il round-trip solo in quel caso.
        """
        if self.current is not None:
            return None
        prossimo = next((s for s in self.steps if s.status == TODO), None)
        if prossimo is not None:
            prossimo.status = DOING
        return prossimo

    def _require(self, step_id: str) -> PlanStep:
        step = self.get(step_id)
        if step is None:
            disponibili = ", ".join(s.id for s in self.steps) or "(nessuno)"
            raise PlanError(
                f"Nel piano non c'e' nessun punto '{step_id}'. Punti: {disponibili}."
            )
        return step


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_block(plan: Plan, *, steps_left: int | None = None) -> str:
    """Blocco di contesto con lo stato del piano, da mettere **in coda**.

    Perche' in coda e non insieme all'``<environment>``: il prefisso del prompt
    -- system, environment -- e' byte-identico fra un passo e l'altro, ed e'
    quello che permette a Ollama di riusare il KV cache invece di rivalutare il
    prompt ogni volta. Il piano invece cambia proprio quando il modello lavora.
    Metterlo in testa significherebbe invalidare la cache ad ogni ``complete``,
    cioe' pagare un prompt eval intero per aggiornare tre parole. In coda la
    divergenza cade dove il contesto stava cambiando comunque (i risultati dei
    tool appena eseguiti), e non costa niente in piu'.

    Va emesso come messaggio ``user``, non ``system``: ``to_ollama_messages``
    fonde tutti i blocchi di sistema in uno solo **in testa**, quindi un
    system in coda finirebbe nel prefisso e la cache salterebbe lo stesso.
    """
    if not plan:
        return ""

    # I punti chiusi restano nel piano per sempre, ma nel blocco che il modello
    # rilegge ad ogni passo entrano solo gli ultimi: la storia serve a non
    # rifare le cose, e per quello bastano gli ultimi quattro piu' un conteggio.
    # Stamparli tutti farebbe crescere il costo di ogni passo con la lunghezza
    # della conversazione -- il difetto che il piano esisteva per togliere.
    chiusi = plan.closed_steps
    nascosti = max(0, len(chiusi) - CLOSED_IN_BLOCK)
    mostrati = set(map(id, chiusi[nascosti:]))

    righe = []
    if nascosti:
        righe.append(f"({nascosti} punti chiusi prima di questi, non ripeterli)")
    for step in plan.steps:
        if step.status in (DONE, SKIPPED) and id(step) not in mostrati:
            continue
        nota = f"  -> {step.note}" if step.note else ""
        righe.append(f"{step.id}. [{_LABELS[step.status]}] {step.text}{nota}")

    coda = ["<piano_di_lavoro>", *righe, "</piano_di_lavoro>", ""]

    corrente = plan.current
    if corrente is not None:
        coda.append(
            f"Stai lavorando al punto {corrente.id}. Fai la prossima azione "
            "concreta per questo punto e basta: non ripianificare, il piano ce "
            "l'hai qui sopra. Quando l'ultima azione del punto lo conclude, "
            "mettici accanto manage_plan action='complete' nello **stesso "
            "passo**: chiudere il punto non e' un passo a se', e il punto "
            "successivo si apre da solo."
        )
    elif plan.open_steps:
        prossimo = plan.open_steps[0]
        coda.append(
            f"Nessun punto aperto. Il prossimo e' il {prossimo.id}: aprilo con "
            "manage_plan action='start' e lavora solo su quello."
        )
    else:
        coda.append(
            "Tutti i punti sono chiusi. Se il lavoro e' davvero finito, scrivi "
            "il messaggio di chiusura; se qualcosa e' rimasto fuori, aggiungilo "
            "al piano invece di farlo di nascosto."
        )

    if steps_left is not None:
        # Il modello pianificava come se avesse tempo infinito perche' non
        # sapeva di averne poco: questo numero e' l'unica cosa che glielo dice.
        coda.append(
            f"Passi agentici rimasti in questo turno: {steps_left}. "
            "Se non bastano, chiudi con quello che hai e dillo."
        )

    return "\n".join(coda)


def render_summary(plan: Plan) -> str:
    """Riga di stato compatta per il messaggio di chiusura e per i log."""
    if not plan:
        return "nessun piano"
    c = plan.counts()
    return (
        f"{c[DONE]}/{len(plan.steps)} fatti"
        + (f", {c[SKIPPED]} saltati" if c[SKIPPED] else "")
        + (f", {c[TODO] + c[DOING]} aperti" if (c[TODO] + c[DOING]) else "")
    )
