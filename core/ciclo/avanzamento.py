"""Monitor di avanzamento: il turno misura se sta andando da qualche parte.

Perche' esiste
--------------
Le reti contro lo stallo che c'erano gia' -- ripetizione, stallo, oscillazione,
loop -- riconoscono ciascuna **una forma** di giro a vuoto (la stessa chiamata
tre volte, lo stesso comando rosso, lo stesso file che torna indietro) e
scattano **una volta per turno**. Un modello che gira a vuoto variando la forma
-- un ``pytest -q`` e poi un ``pytest -q -x``, una ricerca e poi un'altra, la
stessa lettura dopo una scrittura inutile -- non ne incontra nessuna, e consuma
i passi fino al tetto. Nei log di agosto 27 turni sono finiti esattamente al
tetto (30 o 20) e 54 messaggi dell'utente sono un "continua"; oggi il tetto
nelle impostazioni e' 100.

Qui non si guarda la forma delle chiamate ma il loro **effetto**: a ogni passo
si chiede se e' successa almeno una cosa verificabile che prima non c'era. Una
lettura di un file mai letto (o riletto dopo che e' cambiato), una scrittura
che porta un file a un contenuto mai visto nel turno, una verifica che passa a
verde per la prima volta, un punto del piano chiuso, una ricerca che trova
qualcosa di nuovo, un'esplorazione delegata che torna con un referto. Se per
``RIORIENTA_DOPO`` passi di fila non succede niente, l'harness lo dice con i
fatti e offre tre uscite; se si arriva a ``CHIUDI_DOPO``, chiude il turno con
un riepilogo invece di lasciarlo girare.

Riferimenti: lo StuckDetector di OpenHands (forme ripetute) e la regola di
Anthropic sui compiti lunghi ("progresso incrementale verificabile"). La
scelta di misurare l'effetto e non la forma e' di questo progetto: e' la
stessa regola per cui il pensiero si regola su cio' che e' gia' successo.

Le soglie sono costanti con il loro perche', non impostazioni: 6 e 10 stanno
sopra la mediana dei passi di un turno misurata sui log (3) e sotto il p90 (29),
e un'esplorazione legittima produce progresso a ogni lettura nuova.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Passi di fila senza progresso dopo i quali l'harness lo dice e offre le uscite.
RIORIENTA_DOPO = 6
# ...e dopo i quali chiude il turno con un riepilogo.
CHIUDI_DOPO = 10

# Comandi che non dicono niente di nuovo quando riescono: elencare, stampare un
# file, cercare con grep. Un loro successo non e' progresso.
_COMANDI_MUTI = frozenset({
    "ls", "dir", "cat", "head", "tail", "echo", "pwd", "which", "type", "find",
    "grep", "rg", "wc", "tree", "stat", "file", "less", "more",
})


@dataclass(frozen=True, slots=True)
class Chiamata:
    """Una chiamata eseguita nel passo, con il suo esito gia' letto."""

    nome: str
    args: dict[str, Any]
    ok: bool
    esito: dict[str, Any]


def leggi_esito(risultato: str) -> dict[str, Any]:
    try:
        dati = json.loads(risultato)
    except (TypeError, ValueError):
        return {}
    return dati if isinstance(dati, dict) else {}


@dataclass
class Avanzamento:
    """Passi senza progresso e memoria di cio' che e' gia' stato visto."""

    senza_progresso: int = 0
    passi: int = 0
    ultimo_progresso: int = 0
    riorientato: bool = False
    # File letti da quando sono stati scritti l'ultima volta.
    letti: set[str] = field(default_factory=set)
    impronte: dict[str, set[str]] = field(default_factory=dict)
    ricerche: set[str] = field(default_factory=set)
    comandi_riusciti: set[str] = field(default_factory=set)
    esterni: set[str] = field(default_factory=set)
    piani: set[str] = field(default_factory=set)
    # Le ultime chiamate, per il testo del sollecito: dire *cosa* non ha
    # prodotto niente vale piu' di dire che non ha prodotto niente.
    ultime: list[str] = field(default_factory=list)

    # --- registrazione ------------------------------------------------------
    def registra_passo(self, passo: int, chiamate: Iterable[Chiamata]) -> bool:
        """Registra il passo e dice se ha fatto progredire il lavoro."""
        self.passi += 1
        progresso = False
        for c in chiamate:
            if self._e_progresso(c):
                progresso = True
            self.ultime.append(_descrivi(c))
        self.ultime = self.ultime[-5:]
        if progresso:
            self.senza_progresso = 0
            self.ultimo_progresso = passo
            self.riorientato = False
        else:
            self.senza_progresso += 1
        return progresso

    def passo_a_vuoto(self, descrizione: str = "") -> None:
        """Un passo senza chiamate eseguite (sollecitato, interrotto, troncato)."""
        self.passi += 1
        self.senza_progresso += 1
        if descrizione:
            self.ultime = [*self.ultime, descrizione][-5:]

    def _e_progresso(self, c: Chiamata) -> bool:
        if not c.ok:
            return False
        nome, args, esito = c.nome, c.args, c.esito
        if nome in ("write_file", "edit_file"):
            percorso = str(esito.get("filepath") or args.get("filepath") or "")
            impronta = str(esito.get("sha256") or "")
            if not percorso:
                return False
            self.letti.discard(percorso)
            visti = self.impronte.setdefault(percorso, set())
            nuovo = bool(impronta) and impronta not in visti
            if impronta:
                visti.add(impronta)
            return nuovo
        if nome == "read_file":
            percorso = str(esito.get("filepath") or args.get("filepath") or "")
            # "invariato" e' la cache delle riletture: lo stesso contenuto di
            # prima, cioe' niente di nuovo per definizione.
            if not percorso or esito.get("status") == "invariato":
                return False
            chiave = f"{percorso}:{args.get('start_line', '')}-{args.get('end_line', '')}"
            if chiave in self.letti:
                return False
            self.letti.add(chiave)
            self.letti.add(percorso)
            return True
        if nome == "search_files":
            chiave = json.dumps(args, sort_keys=True, ensure_ascii=False)
            if chiave in self.ricerche:
                return False
            self.ricerche.add(chiave)
            trovati = esito.get("match_count")
            if isinstance(trovati, int):
                return trovati > 0
            elenco = esito.get("files") or esito.get("matches") or []
            return any(str(voce) != "(nessuna corrispondenza)" for voce in elenco)
        if nome == "list_files":
            chiave = json.dumps(args, sort_keys=True, ensure_ascii=False)
            nuovo = chiave not in self.ricerche
            self.ricerche.add(chiave)
            return nuovo
        if nome == "run_command":
            if esito.get("esito") != "ok":
                return False
            comando = " ".join(str(esito.get("command") or args.get("command") or "").split())
            primo = comando.split(" ", 1)[0].rsplit("/", 1)[-1] if comando else ""
            if primo in _COMANDI_MUTI or comando in self.comandi_riusciti:
                return False
            self.comandi_riusciti.add(comando)
            return True
        if nome == "manage_plan":
            azione = str(args.get("action") or "").strip().lower()
            if azione in ("complete", "skip"):
                return True
            if azione in ("set", "add"):
                chiave = json.dumps(args.get("steps") or args, sort_keys=True, ensure_ascii=False)
                nuovo = chiave not in self.piani
                self.piani.add(chiave)
                return nuovo
            return False
        if nome in ("esplora", "web_search", "wiki_search", "preview"):
            chiave = nome + json.dumps(args, sort_keys=True, ensure_ascii=False)
            nuovo = chiave not in self.esterni
            self.esterni.add(chiave)
            return nuovo
        return False

    # --- lettura ------------------------------------------------------------
    @property
    def da_riorientare(self) -> bool:
        return self.senza_progresso >= RIORIENTA_DOPO and not self.riorientato

    @property
    def da_chiudere(self) -> bool:
        return self.senza_progresso >= CHIUDI_DOPO


def _descrivi(c: Chiamata) -> str:
    args = c.args or {}
    dettaglio = (
        args.get("filepath") or args.get("command") or args.get("pattern")
        or args.get("action") or args.get("query") or ""
    )
    dettaglio = " ".join(str(dettaglio).split())
    if len(dettaglio) > 60:
        dettaglio = dettaglio[:57] + "..."
    esito = "ok" if c.ok else "fallita"
    return f"{c.nome}({dettaglio}) {esito}" if dettaglio else f"{c.nome} {esito}"
