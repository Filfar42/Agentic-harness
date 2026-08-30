"""Estratto del pensiero: cosa si salva di un punto di piano quando si chiude.

Il problema
-----------
Il ragionamento e' l'unico posto in cui il modello tiene lo stato del lavoro, e
viene buttato via ad ogni turno (``strip_think_from_context``). ``core/plan.py``
esiste proprio per questo: sposta *lo stato* fuori dal pensiero, a cinquanta
token invece di ottomila. Ma lo stato non e' tutto quello che c'e' dentro.

Misurato il 23/08/2026 su quattro sessioni ``qwen3.8:27b`` (350 passi):
2.427.071 caratteri pensati contro 108.970 di risposte, **22 a 1**. E la forma
di quel pensiero dice che non e' scarto: gli 8-grammi ripetuti dentro un blocco
sono lo 0,5%, la somiglianza fra blocchi consecutivi ha mediana 0,15. Il
modello **non si riscrive addosso, delibera** -- e deliberando verifica cose:
errori veri, versioni, percorsi, firme, comandi che funzionano. Quei fatti non
sono scritti da nessun'altra parte, e a fine passo scompaiono.

Perche' non si tiene il blocco
------------------------------
Perche' e' 22 volte le risposte, e la parte che vale la pena conservare e' una
frazione: il 10,1% dei caratteri sta in frasi di ripensamento ('wait',
'actually'), un altro 5,9% in auto-istruzioni. Tenere il grezzo vorrebbe dire
rimettere in contesto proprio cio' che il contesto piccolo doveva togliere. Si
distilla, e si distilla **una volta sola**.

Perche' alla chiusura di un punto
---------------------------------
Su questo progetto la differenza fra un rito e un invito e' misurata:
``manage_plan action='complete'`` **pretende** una nota e ne ha ottenute 23 su
24 in tre sessioni; ``manage_notes``, che la propone, e' stato usato 0 volte su
42 conversazioni. Un'estrazione appesa a un passaggio obbligato avviene;
un'estrazione offerta al modello, no. In piu' costa una chiamata per punto
invece che per passo, e non si infila in mezzo al turno -- dove il watchdog
gia' interrompe 46 volte su 350 passi.

Vale anche su ``skip``: un punto abbandonato e' il posto piu' probabile in cui
nasce uno ``SCARTATO``, cioe' esattamente il fatto che serve a non rifare la
strada. L'origine viene scritta nella voce, perche' un punto saltato dopo una
verifica rossa non deve rileggersi come una conclusione.

Chi chiama chi
--------------
Questo modulo non conosce ne' i tool ne' la UI, e riceve ``backend`` e
``params`` come parametri: e' ``agent`` a conoscere ``pensiero``, non il
contrario -- stessa regola gia' applicata a ``delega`` e ``compaction``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from .prompts import PROMPT_ESTRATTO_PENSIERO
from .textutils import chars_for_tokens, estrai_think, smart_truncate, strip_think

# Tetto all'estratto. Piu' stretto del riassunto di compattazione (700) perche'
# copre un punto solo di piano, non un tratto intero di conversazione: se ne
# servono di piu', il punto era troppo grosso e il problema sta nel piano.
MAX_TOKEN_ESTRATTO = 500

# Quanto pensiero grezzo si manda in pasto all'estrazione, in frazione della
# finestra. Un punto lungo puo' aver prodotto centomila caratteri: mandarli
# tutti significherebbe pagare per la distillazione piu' di quanto la
# distillazione fa risparmiare.
QUOTA_PENSIERO = 0.25

# Sotto questa soglia non si chiama nessuno. Un punto chiuso dopo due passi di
# pensiero corto non ha dentro niente che valga una chiamata al modello: la
# nota che ``complete`` ha gia' preteso dice quanto c'era da dire.
MIN_CHARS_PENSIERO = 1_500

# La risposta convenuta quando non e' rimasto niente da conservare. Va
# riconosciuta e non archiviata: una voce che dice "niente" sporca l'indice, e
# l'indice e' cio' che il modello legge ad ogni passo.
NIENTE = "NIENTE"


def blocchi_del_punto(
    ui_messages: Sequence[dict[str, Any]], punto_id: str
) -> list[str]:
    """Il pensiero grezzo dei passi appartenuti a un punto di piano.

    L'appartenenza si legge dalla traccia ``think`` che ``agent.marca_pensiero``
    attacca al messaggio dell'assistente -- campo ``punto``. Non serve nessun
    buffer: il testo del pensiero **resta nel messaggio in sessione**, perche'
    lo strip avviene in ``build_api_messages`` e non in scrittura.

    Si prendono solo i blocchi grezzi. Mai un estratto o un riassunto
    precedente: e' la stessa regola per cui ``trascrizione(...,
    archiviato=True)`` non fa rientrare il riassunto vecchio -- riassumere un
    riassunto e' il modo in cui un fatto diventa la fotocopia di una fotocopia.
    """
    voluto = str(punto_id).strip()
    if not voluto:
        return []
    fuori: list[str] = []
    for msg in ui_messages:
        if msg.get("role") != "assistant":
            continue
        traccia = msg.get("think")
        if not isinstance(traccia, dict):
            continue
        if str(traccia.get("punto") or "") != voluto:
            continue
        testo = estrai_think(str(msg.get("content") or ""))
        if testo:
            fuori.append(testo)
    return fuori


def estrai(
    blocchi: Sequence[str],
    *,
    punto: str,
    backend: Any,
    params: Any,
) -> str:
    """Distilla ``SCOPERTO``/``SCARTATO`` dal pensiero di un punto.

    Una chiamata sola, senza tool e senza pensiero: qui non c'e' niente da
    decidere, c'e' da separare i fatti verificati dalla deliberazione che li ha
    prodotti. Un'estrazione mancata non deve mai far fallire la chiusura del
    punto, che e' il lavoro vero: si torna stringa vuota e il piano si chiude
    come prima.
    """
    grezzo = "\n\n---\n\n".join(b.strip() for b in blocchi if b and b.strip()).strip()
    if len(grezzo) < MIN_CHARS_PENSIERO:
        return ""

    finestra = int(getattr(params, "num_ctx", 0) or 0)
    if finestra > 0:
        grezzo = smart_truncate(
            grezzo,
            chars_for_tokens(finestra * QUOTA_PENSIERO),
            label="ragionamento del punto",
        )

    p = replace(
        params,
        think=False,
        temperature=0.1,
        max_tokens=min(
            int(getattr(params, "max_tokens", 2048) or 2048), MAX_TOKEN_ESTRATTO
        ),
    )
    messaggi = [
        {"role": "system", "content": PROMPT_ESTRATTO_PENSIERO},
        {
            "role": "user",
            "content": f"Punto di lavoro: {punto}\n\nRagionamento:\n{grezzo}",
        },
    ]
    pezzi: list[str] = []
    try:
        for evento in backend.stream(messaggi, None, p):
            if evento.kind == "content":
                pezzi.append(evento.text)
            elif evento.kind == "error":
                return ""
    except Exception:  # noqa: BLE001
        return ""

    testo = strip_think("".join(pezzi)).strip()
    return "" if _e_niente(testo) else testo


def _e_niente(testo: str) -> bool:
    """Il modello ha detto che non c'era niente da conservare.

    Si accetta anche la forma sporca ("NIENTE.", "niente") perche' il costo di
    sbagliare e' asimmetrico: archiviare una voce vuota sporca l'indice che il
    modello rilegge ad ogni passo, buttarne una che diceva davvero "niente" non
    costa nulla.
    """
    return not testo or testo.strip().strip(".").upper() == NIENTE
