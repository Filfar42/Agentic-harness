"""Lo stato di un turno, in un oggetto solo.

Stava in una trentina di variabili locali di ``run_turn``: flag ``*_nudged``,
contatori, il budget di servizio, la fase del pensiero, la stima dei token.
Funzionava, ma per sapere se due reti potevano scattare nello stesso passo
bisognava leggere mille righe, e l'unico modo di provarne l'interazione era
far girare un turno intero. Qui lo stato e' un dato: le reti di sicurezza
(``core/ciclo/reti.py``) lo leggono, il ciclo lo aggiorna, e alla fine del
turno se ne fa un'istantanea per il turno dopo (``core/ciclo/ripresa.py``).

Dentro ci sono anche i due rilevatori di stallo che prima vivevano in
``agent.py`` -- ``RipetizioniTool`` e ``RitorniDeiFile`` -- con gli stessi
nomi: ``agent`` li ri-esporta.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .avanzamento import Avanzamento

# I tool con cui si guarda e basta. Un passo fatto solo di questi e' una mossa
# di esplorazione, e l'esplorazione e' esattamente cio' che si puo' delegare.
TOOL_ESPLORATIVI = ("read_file", "search_files", "list_files")


def _firma(name: str, args: dict[str, Any] | None) -> tuple[str, str]:
    """Identita' di una chiamata a tool.

    Gli argomenti si normalizzano ordinandoli: ``{"a":1,"b":2}`` e
    ``{"b":2,"a":1}`` sono la stessa chiamata, e un modello che rigenera il JSON
    non li mette sempre nello stesso ordine.
    """
    try:
        return (name, json.dumps(args or {}, sort_keys=True, ensure_ascii=False))
    except (TypeError, ValueError):
        return (name, repr(args))


class RipetizioniTool:
    """Chiamate identiche ripetute nello stesso turno.

    Il modo di guasto piu' comune di un agente non e' il comando che fallisce
    -- per quello c'e' ``VerificationTracker`` -- ma la chiamata che **riesce**
    e che il modello rifa' perche' non ha usato il risultato. Costa un passo e
    una seconda copia dello stesso contenuto in contesto, e nessuna delle altre
    difese la vede: il tracker guarda solo ``run_command``, e
    ``esplorazioni_di_fila`` conta le esplorazioni senza accorgersi che sono la
    stessa.
    """

    __slots__ = ("_falliti", "_storico_falliti", "_viste")

    # Alla terza, non alla seconda: rileggere un file dopo averlo modificato e'
    # legittimo, e sollecitare li' sarebbe rumore su un comportamento corretto.
    SOGLIA = 3

    # Stessa soglia per le chiamate che **falliscono**, e serviva un contatore
    # separato. Le fallite non entravano affatto nel conteggio -- ``registra``
    # veniva chiamata solo dentro un ``if ok:`` -- col ragionamento che rifare
    # una chiamata fallita e' legittimo. Lo e', due volte: un timeout, un
    # container che parte lento. Non lo e' alla terza, e il caso che ha fatto
    # girare a vuoto un turno intero era proprio questo: ``manage_plan``
    # rifiutato per una verifica rossa, richiamato identico, rifiutato di nuovo.
    SOGLIA_STALLO = 3

    def __init__(self) -> None:
        self._viste: dict[tuple[str, str], int] = {}
        self._falliti: dict[tuple[str, str], int] = {}
        # Tutti i fallimenti del turno, anche quelli "dimenticati" da una
        # scrittura riuscita: servono al passaggio di consegne, che deve dire
        # quali strade sono gia' state provate senza esito.
        self._storico_falliti: dict[tuple[str, str], int] = {}

    def registra(self, name: str, args: dict[str, Any] | None) -> int:
        """Quante volte questa esatta chiamata e' gia' stata fatta nel turno."""
        firma = _firma(name, args)
        self._viste[firma] = self._viste.get(firma, 0) + 1
        return self._viste[firma]

    def registra_fallita(self, name: str, args: dict[str, Any] | None) -> int:
        """Quante volte **di fila** questa chiamata e' gia' fallita nel turno."""
        firma = _firma(name, args)
        self._falliti[firma] = self._falliti.get(firma, 0) + 1
        self._storico_falliti[firma] = self._storico_falliti.get(firma, 0) + 1
        return self._falliti[firma]

    def falliti_ripetuti(self, minimo: int = 2) -> list[tuple[tuple[str, str], int]]:
        """Le chiamate fallite almeno ``minimo`` volte nel turno, le piu' insistenti prima."""
        return sorted(
            ((firma, volte) for firma, volte in self._storico_falliti.items() if volte >= minimo),
            key=lambda coppia: -coppia[1],
        )

    def dimentica_letture(self) -> None:
        """Una scrittura invalida le letture: dopo, rileggere ha senso."""
        self._viste = {
            k: v for k, v in self._viste.items() if k[0] not in TOOL_ESPLORATIVI
        }

    def dimentica_fallimenti(self) -> None:
        """Dopo una modifica riuscita, ritentare non e' piu' stallo.

        E' la meta' che rende il contatore dei fallimenti un rilevatore di
        stallo invece di un tetto ai tentativi: quello che conta non e' quante
        volte hai riprovato, e' se fra un tentativo e l'altro e' cambiato
        qualcosa.
        """
        self._falliti.clear()


class RitorniDeiFile:
    """File che tornano a un contenuto gia' avuto nello stesso turno.

    E' il ping-pong dello StuckDetector di OpenHands ("two different
    action-observation pairs alternate"), nella forma in cui lo fa un modello
    piccolo che scrive codice: ``edit_file`` da A a B, il test fallisce,
    ``edit_file`` da B ad A, il test fallisce in un altro modo, di nuovo da A a
    B. Ogni modifica riesce, quindi ``RipetizioniTool`` azzera i suoi contatori
    a ogni passo e lo stallo non si vede. L'impronta del contenuto dopo la
    scrittura -- che ``write_file`` ed ``edit_file`` restituiscono gia' come
    ``sha256`` -- lo rende visibile senza rileggere il disco.
    """

    __slots__ = ("_storia",)

    # Al secondo ritorno: un ritorno solo e' un "annulla" legittimo.
    SOGLIA = 2

    def __init__(self) -> None:
        self._storia: dict[str, tuple[list[str], int]] = {}

    def registra(self, risultato: str) -> tuple[str, int] | None:
        """``(file, ritorni)`` dopo una scrittura riuscita, o None se illeggibile."""
        try:
            esito = json.loads(risultato)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(esito, dict):
            return None
        percorso, impronta = esito.get("filepath"), esito.get("sha256")
        if not isinstance(percorso, str) or not isinstance(impronta, str):
            return None
        viste, ritorni = self._storia.get(percorso, ([], 0))
        # Tornare al contenuto **immediatamente** precedente non e' possibile
        # (sarebbe una scrittura che non cambia niente); qualunque altro gia'
        # visto e' un passo indietro.
        if impronta in viste[:-1]:
            ritorni += 1
        viste.append(impronta)
        self._storia[percorso] = (viste, ritorni)
        return percorso, ritorni

    def file_scritti(self) -> list[str]:
        """I file che il turno ha scritto con successo."""
        return list(self._storia)

    def gia_visto(self, percorso: str, impronta: str) -> bool:
        """Il file e' gia' stato a questo contenuto, in questo turno?"""
        viste, _ = self._storia.get(percorso, ([], 0))
        return impronta in viste


@dataclass
class StatoTurno:
    """Tutto quello che il ciclo ricorda dentro un turno.

    I nomi delle reti in ``scatti`` sono quelli di ``core/ciclo/reti.py``; i
    nomi in ``solleciti`` sono quelli della telemetria (``usage["nudges"]``),
    che restano quelli di sempre perche' li leggono gli script di analisi.
    """

    max_passi: int
    max_passi_di_servizio: int
    passi_di_servizio: int = 0
    servizio_esaurito: bool = False
    # Quante volte ogni rete e' scattata (per i limiti) e con che nome e'
    # stata contata (per la telemetria). Due dizionari perche' una rete puo'
    # contare con nomi diversi: la verifica rossa conta ``verify`` o ``loop``.
    scatti: dict[str, int] = field(default_factory=dict)
    solleciti: dict[str, int] = field(default_factory=dict)
    tools_used: bool = False
    # Scritture riuscite nel turno (write_file/edit_file): la prova che la
    # rete ``senza_prova`` cerca prima di accettare "fatto".
    scritture_riuscite: int = 0
    # --- rilevatori dopo i tool -------------------------------------------
    ripetizioni: RipetizioniTool = field(default_factory=RipetizioniTool)
    ritorni: RitorniDeiFile = field(default_factory=RitorniDeiFile)
    ripetizione_dovuta: tuple[str, int] | None = None
    stallo_dovuto: tuple[str, int] | None = None
    oscillazione_dovuta: tuple[str, int] | None = None
    esplorazioni_di_fila: int = 0
    # Passi di fila con una sola lettura: il segnale del promemoria di batch.
    letture_singole_di_fila: int = 0
    gate_mostrato: bool = False
    multi_step: bool = False
    # --- generazione ------------------------------------------------------
    watchdog_fires: int = 0
    riprese_stream: int = 0
    avvisato_finestra: bool = False
    fattore_stima: float = 1.0
    compattazione_ferma_fino_a: int = 0
    phase_key: tuple[str, str] | None = None
    phase_step: int = 0
    retry_reasoning: bool = False
    # --- avanzamento ------------------------------------------------------
    avanzamento: Avanzamento = field(default_factory=Avanzamento)
    # Il monitor ha offerto l'uscita ("chiudi dicendo cosa blocca"): da qui una
    # risposta che chiude non viene piu' rimandata indietro dalle reti che
    # pretendono lavoro in piu' (verifica rossa, piano aperto, copertura).
    # Senza, l'harness offriva un'uscita e poi la murava.
    uscita_concessa: bool = False

    # --- budget di servizio ------------------------------------------------
    @property
    def servizio_disponibile(self) -> bool:
        return self.passi_di_servizio < self.max_passi_di_servizio

    def conta(self, nome: str) -> None:
        """Un sollecito in piu' nella telemetria."""
        self.solleciti[nome] = self.solleciti.get(nome, 0) + 1

    def scattata(self, rete: str) -> int:
        return self.scatti.get(rete, 0)

    def segna(self, rete: str) -> None:
        self.scatti[rete] = self.scatti.get(rete, 0) + 1

    # --- istantanea per il turno dopo -------------------------------------
    def istantanea(self, *, motivo: str, passi: int, verifiche: Any = None) -> dict[str, Any]:
        """Cio' che del turno vale la pena passare al turno successivo.

        Solo **fatti** ricostruiti dall'harness (esiti, impronte, conteggi),
        mai testo del modello: e' il criterio del progetto su cosa si puo'
        conservare, e quello della letteratura sui passaggi di consegne
        ("stato verificabile a macchina, non prosa").
        """
        falliti = [
            {"tool": nome, "argomenti": _breve(argomenti), "volte": volte}
            for (nome, argomenti), volte in self.ripetizioni.falliti_ripetuti()
        ]
        pendenti = []
        if verifiche is not None:
            pendenti = [v.to_dict() for v in getattr(verifiche, "pendenti", [])]
        return {
            "versione": 1,
            "motivo": motivo,
            "passi": passi,
            "verifiche_rosse": pendenti,
            "falliti": falliti[:8],
            "scritti": sorted(self.ritorni.file_scritti())[:20],
            "solleciti": dict(self.solleciti),
            "senza_progresso": self.avanzamento.senza_progresso,
        }


def _breve(argomenti: str, limite: int = 120) -> str:
    """Gli argomenti di una chiamata in una riga leggibile."""
    try:
        dati = json.loads(argomenti)
    except (TypeError, ValueError):
        return str(argomenti)[:limite]
    if isinstance(dati, dict):
        pezzi = []
        for chiave, valore in dati.items():
            testo = valore if isinstance(valore, str) else json.dumps(valore, ensure_ascii=False)
            if len(testo) > 60:
                testo = testo[:57] + "..."
            pezzi.append(f"{chiave}={testo}")
        return " ".join(pezzi)[:limite]
    return str(dati)[:limite]
