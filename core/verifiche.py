"""Registro delle verifiche del turno.

Sostituisce il vecchio ``VerificationTracker``, che teneva un dizionario
``comando -> (tentativi, exit code)`` e sapeva fare due cose: dire qual era il
rosso piu' insistente, e **dimenticarli tutti insieme**. Erano due limiti, non
uno.

Il primo e' l'identita'. La chiave era la stringa del comando cosi' com'era
stata scritta, quindi ``pytest tests/test_bug.py -q`` e
``python -m pytest tests/test_bug.py -q`` erano due verifiche diverse: la
seconda tornava verde e la prima restava rossa per sempre, su un progetto in
cui nessuno aveva piu' intenzione di rilanciarla. Qui l'identita' e' il comando
**normalizzato** -- via i prefissi (``uv run``, ``python -m``), via le opzioni
che cambiano solo come si legge l'output, argomenti in ordine -- e le forme
testuali viste finiscono in ``varianti``, dove restano leggibili.

Il secondo e' l'azzeramento. ``clear()`` svuotava tutto, e le due uscite del
piano -- ``ignore_red`` e ``skip`` -- la chiamavano: dichiarare non pertinente
**una** verifica ne cancellava anche altre, indipendenti, che nessuno aveva
guardato. Qui si giustifica una verifica per volta, con il motivo attaccato, e
la verifica resta nel registro con lo stato che si e' scelto di darle.

Terzo: non ogni comando che finisce male e' una verifica. Un ``rg`` senza
corrispondenze esce 1 ed e' un risultato, non un guasto; ``pytest`` esce 5
quando non ha raccolto nessun test, che dice che la selezione era sbagliata e
non che il progetto e' rotto. Un registro che li conta produce solleciti a
vuoto -- "rileggi lo stderr e correggi" su un comando che non aveva niente da
correggere -- ed e' uno dei modi in cui il turno gira su se' stesso.

L'ambito, infine, serve a una regola sola: **un sottoinsieme verde non assolve
una suite rossa.** Siccome sono due identita' diverse, la suite resta rossa
finche' non la si rilancia, e questo modulo non deve fare niente di speciale
perche' accada: basta non confonderle.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

# --- stati di una verifica ---------------------------------------------------
ROSSA = "rossa"
VERDE = "verde"
# Rossa, ma l'agente ha dichiarato per iscritto perche' non riguarda il codice
# del progetto. Non sparisce: si legge nel riepilogo al posto di un verde.
GIUSTIFICATA = "giustificata"
# Il comando era sbagliato (non esiste, opzioni errate, nessun test raccolto).
# Non dice niente sul progetto, quindi non e' rossa; ma resta scritto, perche'
# un turno in cui tre verifiche su quattro erano comandi sbagliati non e' un
# turno andato bene.
SBAGLIATA = "sbagliata"

# --- ambito ------------------------------------------------------------------
SUITE = "suite"
PARZIALE = "parziale"
AMBITO_IGNOTO = "ignoto"

# Exit code che segnalano un comando sbagliato, non un progetto rotto.
SHELL_NON_E_UNA_VERIFICA = frozenset({126, 127})

# Codici d'uscita dei runner in stile pytest che significano "hai sbagliato a
# chiamarmi": 4 = errore d'uso, 5 = nessun test raccolto. Trattarli come rossi
# produce il sollecito piu' inutile che ci sia, perche' invita a cercare un bug
# nel codice quando il difetto e' nella riga di comando.
PYTEST_USO_SBAGLIATO = frozenset({4, 5})

# Comandi che non diagnosticano la qualita' del progetto. Un fallimento qui e'
# un esito ("nessuna corrispondenza", "il file non c'e'"), non un guasto.
#
# E' un elenco di esclusione e non di inclusione di proposito: un progetto puo'
# verificarsi con ``./scripts/check.sh`` o con un Makefile, e una lista di
# runner ammessi lascerebbe fuori proprio quelli. Qui si nomina cio' che si sa
# non essere una verifica, e tutto il resto continua a contare come prima.
NON_DIAGNOSTICI = frozenset({
    "rg", "grep", "egrep", "fgrep", "ag", "ack", "find", "fd", "locate",
    "ls", "dir", "tree", "cat", "head", "tail", "less", "more", "sed", "awk",
    "cut", "sort", "uniq", "wc", "which", "where", "whereis", "type",
    "echo", "printf", "pwd", "date", "env", "printenv", "stat", "file",
    "du", "df", "ps", "top", "kill", "sleep", "true", "false", "test", "[",
    "git", "diff", "cmp", "curl", "wget", "ping", "nc", "docker", "docker-compose",
    "tar", "unzip", "zip", "gzip", "cp", "mv", "mkdir", "rmdir", "touch",
    "chmod", "chown", "ln", "xargs", "tee", "man", "help", "history",
})

# Runner di verifica riconosciuti: serve solo a decidere l'ambito e a leggere i
# codici d'uscita in stile pytest. Non e' un permesso: un comando che non e'
# qui dentro resta comunque una verifica se non e' fra i NON_DIAGNOSTICI.
RUNNER_PYTEST = frozenset({"pytest", "py.test"})
RUNNER_TEST = RUNNER_PYTEST | frozenset({
    "unittest", "nose2", "tox", "nox", "jest", "vitest", "mocha", "ava",
    "cargo", "go", "dotnet", "mvn", "gradle", "ctest", "rspec", "phpunit",
    "npm", "yarn", "pnpm", "make",
})
# Strumenti che girano sempre su tutto se non gli si da' un percorso.
RUNNER_STATICI = frozenset({"ruff", "mypy", "pyright", "flake8", "pylint",
                            "eslint", "tsc", "black", "isort"})

# Opzioni che cambiano come si legge l'output, non cosa viene verificato.
# Toglierle dall'identita' evita che ``pytest -q`` e ``pytest -v`` risultino due
# verifiche distinte, cioe' che rilanciare in modo piu' loquace lasci aperto il
# rosso di prima.
OPZIONI_COSMETICHE = frozenset({
    "-q", "-qq", "--quiet", "-v", "-vv", "-vvv", "--verbose", "-s",
    "--no-header", "--no-summary", "--showlocals", "-l", "--color", "--tb",
})
PREFISSI_COSMETICI = ("--tb=", "--color=", "--capture=", "--durations=",
                      "--log-cli-level=", "--show-capture=")

# Prefissi che lanciano qualcos'altro: ``uv run pytest`` verifica quanto
# ``pytest``, e il modello passa dall'uno all'altro senza pensarci.
_WRAPPER_RUN = frozenset({"uv", "poetry", "pipenv", "pdm", "hatch", "rye", "nox"})
_INTERPRETI = re.compile(r"^(python|python3|python3\.\d+|py|pypy|pypy3)(\.exe)?$")

# Operatori di shell riconosciuti **sui token**, non sulla stringa: un `;`
# dentro le virgolette di ``python -c "import sys; sys.exit(1)"`` non e' un
# operatore, e cercarlo nel testo grezzo faceva cadere quel comando nel ramo
# "non so leggerlo" insieme alle pipe vere.
_OPERATORI_TOKEN = frozenset({"&&", "||", ";", "|", ">", ">>", "<", "2>", "2>&1", "&"})
_CD_INIZIALE = re.compile(r"^\s*cd\s+[^&;]+(&&|;)\s*")

# Argomenti che nominano tutto il progetto: non restringono niente, quindi non
# trasformano una suite in una selezione. ``ruff check .`` e ``go test ./...``
# sono la suite intera, e contarli come sottoinsieme significherebbe non
# accorgersi mai che la suite e' rossa.
NON_RESTRINGONO = frozenset({".", "./", "./...", "...", "*"})


def _senza_cd(comando: str) -> str:
    """``cd /work && pytest`` verifica quanto ``pytest``.

    E' la forma che i modelli scrivono quando non si fidano della directory di
    lavoro, ed e' gia' costata un rosso immortale: il ``cd`` davanti rendeva la
    riga un'identita' nuova ogni volta che cambiava il percorso.
    """
    precedente = None
    while precedente != comando:
        precedente = comando
        comando = _CD_INIZIALE.sub("", comando, count=1)
    return comando.strip()


def _tokenizza(comando: str) -> list[str] | None:
    """Token del comando, oppure ``None`` se non e' una riga semplice.

    ``None`` vuol dire "non so leggerlo": pipe, redirezioni, sottoshell. In quel
    caso l'identita' resta la stringa intera normalizzata negli spazi, che e'
    esattamente il comportamento di prima -- si perde la normalizzazione, non la
    verifica.
    """
    try:
        token = shlex.split(comando, posix=True)
    except ValueError:
        return None
    if any(t in _OPERATORI_TOKEN for t in token):
        return None
    return token or None


def _eseguibile(token: str) -> str:
    """Nome nudo del programma: via il percorso e via ``.exe``."""
    nome = token.replace("\\", "/").rsplit("/", 1)[-1]
    if nome.lower().endswith(".exe"):
        nome = nome[:-4]
    return nome


def _scarta_wrapper(token: list[str]) -> list[str]:
    """Toglie i prefissi che lanciano un altro programma."""
    cambiato = True
    while cambiato and len(token) >= 2:
        cambiato = False
        primo = _eseguibile(token[0])
        # variabili d'ambiente in testa: ``PYTHONPATH=. pytest``
        if "=" in primo and not primo.startswith("-"):
            token = token[1:]
            cambiato = True
            continue
        if primo in _WRAPPER_RUN and token[1] == "run":
            token = token[2:]
            cambiato = True
            continue
        if _INTERPRETI.match(primo):
            # ``python -m pytest`` -> ``pytest``; ``py -3 -m pytest`` -> idem.
            resto = token[1:]
            while resto and resto[0].startswith("-") and resto[0] != "-m":
                resto = resto[1:]
            if resto and resto[0] == "-m" and len(resto) >= 2:
                token = resto[1:]
                cambiato = True
                continue
        if primo == "npx":
            token = token[1:]
            cambiato = True
            continue
        if primo in {"npm", "yarn", "pnpm"} and token[1] == "run" and len(token) >= 3:
            # ``npm run test`` e ``npm test`` sono lo stesso script. Il gestore
            # **resta** nell'identita': senza, lo script si chiamerebbe solo
            # "test" e si confonderebbe con qualunque altra cosa abbia quel nome.
            token = [token[0], *token[2:]]
            cambiato = True
            continue
    return token


def _e_cosmetica(arg: str) -> bool:
    return arg in OPZIONI_COSMETICHE or arg.startswith(PREFISSI_COSMETICI)


def identita_e_ambito(comando: str) -> tuple[str, str, str]:
    """``(identita, ambito, runner)`` per un comando.

    L'identita' e' cio' che decide se due esecuzioni parlano della stessa cosa.
    L'ambito distingue la suite intera da una selezione, perche' un verde su
    tre test non assolve una suite rossa.
    """
    grezzo = " ".join((comando or "").split())
    nudo = _senza_cd(grezzo)
    token = _tokenizza(nudo)
    if not token:
        return (nudo or grezzo, AMBITO_IGNOTO, "")

    token = _scarta_wrapper(token)
    if not token:
        return (nudo or grezzo, AMBITO_IGNOTO, "")

    runner = _eseguibile(token[0])
    resto = [a for a in token[1:] if not _e_cosmetica(a)]

    # Gli argomenti si ordinano: ``pytest a b`` e ``pytest b a`` verificano la
    # stessa cosa, e un modello che rigenera la riga non li mette sempre nello
    # stesso ordine.
    identita = " ".join([runner, *sorted(resto)]).strip()

    selettori = {"-k", "-m", "--deselect", "--lf", "--last-failed", "--ff",
                 "--failed-first", "--sw", "--stepwise"}
    ha_selettore = any(a in selettori or a.startswith(("-k=", "-m=", "--deselect="))
                       for a in resto)
    ha_percorso = any(not a.startswith("-") and a not in NON_RESTRINGONO for a in resto)

    if runner in RUNNER_TEST or runner in RUNNER_STATICI:
        # ``cargo test`` / ``go test ./...`` / ``dotnet test``: il sottocomando
        # non e' un percorso, e da solo non restringe niente.
        sottocomandi = {"test", "check", "clippy", "run"}
        percorsi = [a for a in resto
                    if not a.startswith("-")
                    and a not in sottocomandi
                    and a not in NON_RESTRINGONO]
        ambito = PARZIALE if (ha_selettore or percorsi) else SUITE
    else:
        ambito = PARZIALE if (ha_selettore or ha_percorso) else AMBITO_IGNOTO

    return (identita or nudo or grezzo, ambito, runner)


@dataclass
class Verifica:
    """Una cosa che il progetto deve superare, e come e' andata l'ultima volta."""

    identita: str
    comando: str
    ambito: str = AMBITO_IGNOTO
    runner: str = ""
    stato: str = ROSSA
    returncode: int | None = None
    tentativi: int = 0
    motivo: str = ""
    via: str = ""
    varianti: list[str] = field(default_factory=list)

    def vedi(self, comando: str) -> None:
        self.comando = comando
        if comando not in self.varianti:
            self.varianti.append(comando)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "identita": self.identita,
            "comando": self.comando,
            "ambito": self.ambito,
            "stato": self.stato,
            "returncode": self.returncode,
            "tentativi": self.tentativi,
        }
        if self.motivo:
            d["motivo"] = self.motivo
        if self.via:
            d["via"] = self.via
        if len(self.varianti) > 1:
            d["varianti"] = list(self.varianti)
        return d


class RegistroVerifiche:
    """Le verifiche viste in questo turno, con il loro esito.

    Vive nel ciclo agentico (``core/agent.py``), che e' l'unico posto che vede
    passare i risultati dei tool. I tool lo leggono attraverso ``ToolContext``
    e **non lo modificano**: chiudere un punto del piano non cambia com'e'
    andato un test. L'unica scrittura che arriva da fuori e' ``giustifica``,
    che non cancella niente -- attacca un motivo a una verifica sola.
    """

    def __init__(self) -> None:
        self.verifiche: dict[str, Verifica] = {}

    # --- scrittura ----------------------------------------------------------
    def record(self, name: str, result: str) -> Verifica | None:
        """Legge la busta di un tool e aggiorna il registro. Torna la verifica toccata."""
        if name != "run_command":
            return None
        try:
            payload = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        comando = str(payload.get("command") or "").strip()
        if not comando:
            # Le buste d'errore (``{"error": ..., "hint": ...}``) non portano il
            # comando, ed e' cosi' che i guasti d'ambiente -- Docker spento,
            # binario fuori dal recinto, timeout -- restano fuori dal registro:
            # per struttura del dato, non riconoscendo il testo del messaggio.
            return None

        identita, ambito, runner = identita_e_ambito(comando)
        codice = payload.get("returncode")

        if _sembra_un_server(comando):
            # Un server non ha un "esito": non termina per progetto. Se e' qui
            # e' perche' il timeout l'ha ucciso, e trattarlo come verifica rossa
            # produce il ciclo osservato dal vivo -- l'harness ordina di
            # rilanciare, il comando riparte, viene ucciso di nuovo.
            self.verifiche.pop(identita, None)
            return None

        if runner in NON_DIAGNOSTICI or (not runner and _primo_nome(comando) in NON_DIAGNOSTICI):
            # Un ``rg`` senza corrispondenze esce 1 ed e' una risposta.
            self.verifiche.pop(identita, None)
            return None

        verifica = self.verifiche.get(identita)
        if verifica is None:
            verifica = Verifica(identita=identita, comando=comando,
                                ambito=ambito, runner=runner)
            self.verifiche[identita] = verifica
        verifica.vedi(comando)
        verifica.ambito = ambito
        verifica.returncode = codice if isinstance(codice, int) else None

        if codice == 0:
            verifica.stato = VERDE
            verifica.tentativi = 0
            verifica.motivo = ""
            verifica.via = ""
            return verifica

        if codice in SHELL_NON_E_UNA_VERIFICA:
            # 127 = comando inesistente, 126 = trovato ma non eseguibile.
            verifica.stato = SBAGLIATA
            verifica.tentativi = 0
            return verifica

        if runner in RUNNER_PYTEST and codice in PYTEST_USO_SBAGLIATO:
            # 4 = errore d'uso, 5 = nessun test raccolto. La selezione era
            # sbagliata: cercare un bug nel codice e' cercarlo dove non c'e'.
            verifica.stato = SBAGLIATA
            verifica.tentativi = 0
            return verifica

        if not isinstance(codice, int):
            return verifica

        # Una verifica gia' giustificata che torna a fallire **non** ridiventa
        # rossa: il motivo scritto vale finche' non cambia il mondo, e
        # rilanciarla per curiosita' non deve riaprire il blocco. Torna rossa
        # solo se il motivo viene ritirato.
        if verifica.stato == GIUSTIFICATA:
            verifica.tentativi += 1
            return verifica

        verifica.stato = ROSSA
        verifica.tentativi += 1
        return verifica

    def giustifica(self, identita: str | None, motivo: str, via: str = "") -> Verifica | None:
        """Dichiara una verifica non pertinente al codice, con il motivo scritto.

        Una sola, non tutte: era il difetto del vecchio ``clear()``, e si vedeva
        in prova -- ignorare un rosso ne faceva sparire due. Senza ``identita``
        si giustifica la piu' insistente, che e' quella che i messaggi nominano.
        """
        verifica = self.verifiche.get(identita or "") if identita else self.insistente
        if verifica is None:
            return None
        verifica.stato = GIUSTIFICATA
        verifica.motivo = str(motivo or "").strip()
        verifica.via = via
        return verifica

    def clear(self) -> None:
        """Dimentica tutto. Resta per l'inizio di un turno, non per il piano.

        Il piano **non** la chiama piu': chiudere o saltare un punto non cancella
        com'e' andato un test. Cancellarlo era il modo in cui un turno con tre
        rossi archiviati finiva per somigliare a un turno verde.
        """
        self.verifiche.clear()

    # --- lettura ------------------------------------------------------------
    @property
    def pendenti(self) -> list[Verifica]:
        """Le verifiche ancora rosse, dalla piu' insistente alla meno."""
        rosse = [v for v in self.verifiche.values() if v.stato == ROSSA]
        return sorted(rosse, key=lambda v: (-v.tentativi, v.identita))

    @property
    def insistente(self) -> Verifica | None:
        pendenti = self.pendenti
        return pendenti[0] if pendenti else None

    @property
    def failing(self) -> dict[str, tuple[int, int]]:
        """Forma del vecchio tracker: ``comando -> (tentativi, exit code)``.

        Serve al codice e ai test scritti prima del registro. Le chiavi sono i
        comandi come sono stati scritti, non le identita' normalizzate, perche'
        e' quello che quel codice si aspetta di leggere.
        """
        return {v.comando: (v.tentativi, v.returncode if isinstance(v.returncode, int) else 1)
                for v in self.pendenti}

    @property
    def unresolved(self) -> tuple[str, int, int] | None:
        """``(comando, tentativi, exit code)`` della verifica rossa piu' insistente."""
        v = self.insistente
        if v is None:
            return None
        return (v.comando, v.tentativi, v.returncode if isinstance(v.returncode, int) else 1)

    def stato(self) -> str:
        """``verde`` | ``rossa`` | ``giustificata`` | ``non_verificato``."""
        if self.pendenti:
            return "rossa"
        if any(v.stato == VERDE for v in self.verifiche.values()):
            return "verde"
        if any(v.stato == GIUSTIFICATA for v in self.verifiche.values()):
            return "giustificata"
        return "non_verificato"

    def riepilogo(self) -> dict[str, Any]:
        """Lo stato della qualita', separato dall'avanzamento del piano.

        E' quello che l'interfaccia mostra accanto al piano e che il riepilogo
        finale deve dire per intero: senza questo, "separare piano e verifiche"
        vorrebbe dire solo nascondere i rossi meglio di prima.
        """
        per_stato: dict[str, list[dict[str, Any]]] = {}
        for v in self.verifiche.values():
            per_stato.setdefault(v.stato, []).append(v.to_dict())
        return {
            "stato": self.stato(),
            "pendenti": [v.to_dict() for v in self.pendenti],
            "verdi": per_stato.get(VERDE, []),
            "giustificate": per_stato.get(GIUSTIFICATA, []),
            "sbagliate": per_stato.get(SBAGLIATA, []),
            "suite_rossa": any(v.ambito == SUITE for v in self.pendenti),
        }


def _primo_nome(comando: str) -> str:
    grezzo = _senza_cd(" ".join((comando or "").split()))
    primo = grezzo.split(" ", 1)[0] if grezzo else ""
    return _eseguibile(primo)


def _sembra_un_server(comando: str) -> bool:
    # Import ritardato: ``core.tools`` importa questo modulo, e il riconoscimento
    # dei server vive li' insieme al resto delle regole su ``run_command``.
    from core.tools import looks_like_server

    return looks_like_server(comando)
