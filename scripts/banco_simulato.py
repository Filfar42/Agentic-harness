"""Banco simulato: i guasti dei log, riprodotti da un modello finto, contro due versioni dell'harness.

Fase 5 del lavoro del 25/09/2026. Il modello non e' vero: e' una **politica
reattiva** che legge la richiesta che l'harness le manda, sceglie la mossa
successiva di un compito noto e sbaglia con le probabilita' osservate nei log
(modifica che non aggancia, chiamata scritta come testo, chiusura prematura,
dichiarazione senza prova, stallo). Ogni volta che l'harness le risponde con un
sollecito, lo segue con la **stessa** probabilita' qualunque sia il sollecito:
la differenza fra due versioni la fanno i meccanismi dell'harness, non un
modello tarato per premiarne una.

    python scripts/banco_simulato.py --radice . --semi 30 [--json out.json]
    python scripts/banco_simulato.py --radice ../astra-base --semi 30

``--radice`` e' la cartella del codice da provare: lo stesso file gira su
``master`` e sul ramo di lavoro, e i due JSON si confrontano con
``--confronta a.json b.json``.

Cosa misura, per scenario: percentuale di compiti riusciti (criterio
controllato sul disco, non sulla risposta), passi, chiamate al modello, token
di prompt inviati (stima dell'harness), chiamate fallite, solleciti, motivo di
chiusura. Cosa **non** misura: quanto il modello vero reagirebbe ai solleciti.
Il parametro ``--obbedienza`` (default 0,75) e' un'ipotesi dichiarata, e il
rapporto lo fa variare.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import time
import zlib
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Caricamento della versione da provare
# ---------------------------------------------------------------------------


def carica_harness(radice: Path) -> dict[str, Any]:
    radice = radice.resolve()
    for nome in [m for m in list(sys.modules) if m == "core" or m.startswith("core.")]:
        del sys.modules[nome]
    sys.path.insert(0, str(radice))
    moduli = {
        "agent": importlib.import_module("core.agent"),
        "backend": importlib.import_module("core.backend"),
        "config": importlib.import_module("core.config"),
        "tools": importlib.import_module("core.tools"),
        "prompts": importlib.import_module("core.prompts"),
        "textutils": importlib.import_module("core.textutils"),
    }
    sys.path.remove(str(radice))
    return moduli


# ---------------------------------------------------------------------------
# Il modello finto
# ---------------------------------------------------------------------------


@dataclass
class Mossa:
    chiamate: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    testo: str = ""
    pensiero: int = 300               # caratteri di ragionamento
    nel_testo: bool = False           # chiamate scritte come JSON nel canale testuale


@dataclass
class Vista:
    """Quello che il modello finto capisce della richiesta che riceve."""

    messaggi: list[dict[str, Any]]
    risultati: list[tuple[str, dict[str, Any], dict[str, Any]]]   # (tool, args, esito)
    sollecito: str                     # testo dell'ultimo messaggio "user" dopo la sua ultima mossa
    coda: str                          # blocco di coda (piano, verifiche...)
    sue_mosse: int


def _json(testo: Any) -> dict[str, Any]:
    try:
        p = json.loads(testo) if isinstance(testo, str) else testo
    except (TypeError, ValueError):
        return {}
    return p if isinstance(p, dict) else {}


CODA_TAG = ("<piano_di_lavoro>", "<comunicazione_turno>", "<verifiche_aperte>", "<skill",
            "<libreria", "<anteprima", "<note", "<avanzamento", "<memoria", "<stato_wiki")
INFORMAZIONI = ("<ripresa>", "<aggiornamento_workspace>", "<cronologia_compattata>")


def costruisci_vista(messaggi: list[dict[str, Any]], richieste_utente: set[str]) -> Vista:
    """Cosa c'e' nella richiesta, letto come lo leggerebbe il modello.

    Un **sollecito** e' un messaggio ``user`` arrivato dopo l'ultima mossa del
    modello che non e' dell'utente, non e' il blocco di coda e non e' una nota
    informativa dell'harness (ripresa, aggiornamento del workspace).
    """
    args_per_id: dict[str, tuple[str, dict[str, Any]]] = {}
    risultati = []
    ultima_mossa = -1
    for i, m in enumerate(messaggi):
        if m.get("role") == "assistant":
            ultima_mossa = i
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                args_per_id[str(c.get("id"))] = (str(fn.get("name")), _json(fn.get("arguments")))
        elif m.get("role") == "tool":
            nome, args = args_per_id.get(str(m.get("tool_call_id")), (str(m.get("name")), {}))
            risultati.append((nome, args, _json(m.get("content"))))
    sollecito = []
    coda = ""
    # Da dove leggere: dopo l'ultima mossa del modello, o dopo l'ultima
    # richiesta dell'utente se il modello non ha ancora lasciato traccia (un
    # passo buttato dal watchdog non lascia un messaggio assistant).
    inizio = ultima_mossa
    for i, m in enumerate(messaggi):
        if m.get("role") == "user" and str(m.get("content") or "").strip() in richieste_utente:
            inizio = max(inizio, i)
    if inizio >= 0:
        for m in messaggi[inizio + 1:]:
            if m.get("role") != "user":
                continue
            testo = str(m.get("content") or "")
            pulito = testo.strip()
            if pulito in richieste_utente:
                continue
            if pulito.startswith(CODA_TAG):
                coda = testo
            elif pulito.startswith(INFORMAZIONI) or pulito.startswith("[Nota dell'harness]"):
                continue
            else:
                sollecito.append(testo)
    sue = sum(1 for m in messaggi if m.get("role") == "assistant")
    return Vista(messaggi, risultati, "\n".join(sollecito), coda, sue)


class ModelloFinto:
    """Backend compatibile con ``run_turn``: la mossa la sceglie ``politica``."""

    supports_cancellation = False

    def __init__(self, politica: Callable[[Vista, random.Random], Mossa], rng: random.Random,
                 richieste: set[str], stima: Callable[[list[dict[str, Any]]], int], schema_tokens: int):
        self.politica = politica
        self.rng = rng
        self.richieste = richieste
        self.stima = stima
        self.schema_tokens = schema_tokens
        self.chiamate = 0
        self.chiamate_servizio = 0
        self.token_prompt = 0
        self._id = 0

    def stream(self, messages: list[dict[str, Any]], tools: Any, _params: Any = None, **_: Any) -> Iterator[Any]:
        from core.backend import StreamEvent  # la versione caricata

        tok = self.stima(messages) + (self.schema_tokens if tools else 0)
        self.token_prompt += tok
        if not tools:
            # Chiamata di servizio: riepilogo forzato, compattazione, estratto.
            self.chiamate_servizio += 1
            yield StreamEvent("content", text="Fatto: parte del lavoro. Poi: riprendere dal punto aperto.")
            yield StreamEvent("usage", usage={"done_reason": "stop", "prompt_eval_count": tok})
            return
        self.chiamate += 1
        vista = costruisci_vista(messages, self.richieste)
        mossa = self.politica(vista, self.rng)
        if mossa.pensiero:
            blocco = "Sto ragionando sul prossimo passo. " * (mossa.pensiero // 35 + 1)
            for k in range(0, len(blocco), 2000):
                yield StreamEvent("reasoning", text=blocco[k:k + 2000])
        if mossa.nel_testo and mossa.chiamate:
            nome, args = mossa.chiamate[0]
            yield StreamEvent("content", text=json.dumps({"name": nome, "arguments": args},
                                                         ensure_ascii=False))
        else:
            if mossa.testo:
                yield StreamEvent("content", text=mossa.testo)
            for nome, args in mossa.chiamate:
                self._id += 1
                yield StreamEvent("tool_call", tool_call={
                    "id": f"call_{self._id}", "name": nome,
                    "arguments": json.dumps(args, ensure_ascii=False),
                })
        yield StreamEvent("usage", usage={"done_reason": "stop", "prompt_eval_count": tok,
                                          "eval_count": mossa.pensiero // 4 + 50})


# ---------------------------------------------------------------------------
# Scenari
# ---------------------------------------------------------------------------

STATS_PY = '''class Statistiche:
    def __init__(self, valori):
        self.valori = list(valori)

    def media(self):
        if not self.valori:
            return 0.0
        totale = sum(self.valori)
        return totale / (len(self.valori) + 1)
'''
TEST_STATS = '''from stats import Statistiche


def test_media():
    assert Statistiche([2, 4]).media() == 3


def test_vuota():
    assert Statistiche([]).media() == 0.0
'''
RIGA_BUG = "        return totale / (len(self.valori) + 1)"
RIGA_OK = "        return totale / len(self.valori)"
PYTEST = "python -m pytest -q -p no:cacheprovider"


def _obbedisce(v: Vista, rng: random.Random, p: float) -> bool:
    return bool(v.sollecito) and rng.random() < p


def _ultimo(v: Vista, nome: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for n, a, e in reversed(v.risultati):
        if n == nome:
            return a, e
    return None


def _verde(v: Vista) -> bool | None:
    r = _ultimo(v, "run_command")
    if r is None:
        return None
    return r[1].get("esito") == "ok"


def _deriva(rng: random.Random, riga: str) -> str:
    """La riga come la ricorda un modello che non la sta copiando."""
    tipo = rng.choices(["rientro", "spazi_finali", "token"], weights=[35, 25, 40])[0]
    if tipo == "rientro":
        return riga[4:]
    if tipo == "spazi_finali":
        return riga + "  "
    return riga.replace(" + 1)", "+1)")


@dataclass
class Scenario:
    nome: str
    guasto: str
    richiesta: str
    file: dict[str, str]
    politica: Callable[..., Callable[[Vista, random.Random], Mossa]]
    riuscito: Callable[[Path], bool]
    max_passi: int = 24
    turni: list[str] = field(default_factory=list)   # messaggi successivi dell'utente
    max_passi_turni: list[int] = field(default_factory=list)


def _fisso_stats(ws: Path) -> bool:
    testo = (ws / "stats.py").read_text(encoding="utf-8")
    return RIGA_OK in testo and RIGA_BUG not in testo


# --- S1: la modifica che non aggancia (F3) ----------------------------------

def _indice(v: Vista, nome: str, cond: Callable[[dict[str, Any], dict[str, Any]], bool] = lambda _a, _e: True) -> int:
    return max((i for i, (n, a, e) in enumerate(v.risultati) if n == nome and cond(a, e)), default=-1)


def politica_modifica(p_deriva: float, _p_obbedienza: float, p_diagnostica: float):
    """Leggi, modifica, verifica. La riga da sostituire si ricorda male con ``p_deriva``.

    L'obbedienza ai solleciti qui non entra: la correzione viene (o no) dal
    messaggio d'errore di ``edit_file``, con probabilita' ``p_diagnostica``.
    Il parametro resta per avere la stessa firma delle altre politiche.
    """

    def modifica(old: str) -> Mossa:
        return Mossa([("edit_file", {"filepath": "stats.py", "old_string": old, "new_string": RIGA_OK})],
                     pensiero=900)

    def decidi(v: Vista, rng: random.Random) -> Mossa:
        if _verde(v):
            return Mossa(testo="Fatto: corretta la media in stats.py.\nVerifica: pytest verde.", pensiero=200)
        lettura = _indice(v, "read_file")
        if lettura < 0:
            return Mossa([("read_file", {"filepath": "stats.py"})], pensiero=600)
        riuscita = _indice(v, "edit_file", lambda _a, e: e.get("status") == "ok")
        if riuscita >= 0:
            if _indice(v, "run_command") < riuscita:
                return Mossa([("run_command", {"command": PYTEST})], pensiero=200)
            # Test rossi dopo una modifica riuscita: non succede in questo scenario.
            return Mossa(testo="Fatto, ma i test restano rossi.")
        fallita = _indice(v, "edit_file")
        if fallita < 0:
            return modifica(_deriva(rng, RIGA_BUG) if rng.random() < p_deriva else RIGA_BUG)
        esito = v.risultati[fallita][2]
        vicino = esito.get("piu_vicino") or {}
        if vicino and rng.random() < p_diagnostica:
            # Il testo esatto e' nell'errore: si copia da li'.
            return modifica(RIGA_BUG)
        if lettura < fallita:
            return Mossa([("read_file", {"filepath": "stats.py"})], pensiero=500)
        # Riletto: si riprova, con la stessa probabilita' di sbagliare la copia.
        return modifica(_deriva(rng, RIGA_BUG) if rng.random() < p_deriva else RIGA_BUG)
    return decidi


# --- S2: la chiamata scritta come testo (F6) ---------------------------------

def politica_canale(p_testo: float, p_obbedienza: float):
    def decidi(v: Vista, rng: random.Random) -> Mossa:
        verde = _verde(v)
        if verde:
            return Mossa(testo="Fatto: corretta la media.\nVerifica: pytest verde.")
        if not any(n == "read_file" for n, _, _ in v.risultati):
            chiamata = ("read_file", {"filepath": "stats.py"})
        elif not any(n == "edit_file" and e.get("status") == "ok" for n, _, e in v.risultati):
            chiamata = ("edit_file", {"filepath": "stats.py", "old_string": RIGA_BUG, "new_string": RIGA_OK})
        else:
            chiamata = ("run_command", {"command": PYTEST})
        # Dopo un sollecito si ubbidisce con la stessa probabilita' per tutti.
        if v.sollecito:
            nel_testo = not (rng.random() < p_obbedienza)
        else:
            nel_testo = rng.random() < p_testo
        return Mossa([chiamata], nel_testo=nel_testo)
    return decidi


# --- S3: stallo errante, compito impossibile (F7, F1) -------------------------

APP_FANTASMA = "import pacchetto_fantasma\n\n\ndef calcola():\n    return pacchetto_fantasma.valore()\n"
TEST_FANTASMA = "from app import calcola\n\n\ndef test_calcola():\n    assert calcola() == 42\n"
CICLO_STALLO = [
    ("run_command", {"command": PYTEST}),
    ("read_file", {"filepath": "app.py"}),
    ("run_command", {"command": "python -c 'import pacchetto_fantasma'"}),
    ("search_files", {"pattern": "pacchetto_fantasma"}),
    ("read_file", {"filepath": "test_app.py"}),
    ("run_command", {"command": PYTEST + " -x"}),
    ("search_files", {"pattern": "valore"}),
    ("read_file", {"filepath": "app.py"}),
    ("run_command", {"command": "python -c 'import app'"}),
    ("search_files", {"pattern": "import"}),
]


def politica_stallo(p_obbedienza: float):
    uscite = ("chiudi", "cosa manca", "ask_user_question", "chiedi", "cosa blocca", "bloccato")

    def decidi(v: Vista, rng: random.Random) -> Mossa:
        if v.sollecito and any(u in v.sollecito.lower() for u in uscite) and rng.random() < p_obbedienza:
            return Mossa(testo="Bloccato: il modulo pacchetto_fantasma non esiste e non si installa. "
                               "Poi: serve sapere da dove arriva.")
        k = sum(1 for m in v.messaggi if m.get("role") == "assistant" and m.get("tool_calls"))
        nome, args = CICLO_STALLO[k % len(CICLO_STALLO)]
        args = dict(args)
        if nome == "search_files":
            args["pattern"] = f"{args['pattern']}{'' if k < 10 else k // 10}"
        return Mossa([(nome, args)], pensiero=1200)
    return decidi


# --- S3b: esplorazione lunga ma legittima (falsi positivi di A3; A7d) ---------

N_MODULI = 12


def politica_esplora(p_obbedienza: float):
    def decidi(v: Vista, rng: random.Random) -> Mossa:
        letti = {a.get("filepath") for n, a, e in v.risultati if n == "read_file" and "error" not in e}
        verde = _verde(v)
        if verde:
            return Mossa(testo="Fatto: il bug era in m12.py.\nVerifica: pytest verde.")
        if not any(n == "run_command" for n, _, _ in v.risultati):
            return Mossa([("run_command", {"command": PYTEST})])
        mancanti = [f"m{i}.py" for i in range(1, N_MODULI + 1) if f"m{i}.py" not in letti]
        if mancanti:
            quanti = 1
            if "tutti in questo passo" in (v.coda + v.sollecito) and rng.random() < p_obbedienza:
                quanti = 4
            return Mossa([("read_file", {"filepath": f}) for f in mancanti[:quanti]], pensiero=400)
        if not any(n == "edit_file" and e.get("status") == "ok" for n, _, e in v.risultati):
            return Mossa([("edit_file", {"filepath": "m12.py", "old_string": "return x - 1",
                                         "new_string": "return x + 1"})])
        return Mossa([("run_command", {"command": PYTEST})])
    return decidi


# --- S4: chiusura con il piano aperto (F8) -----------------------------------

MODULI_S4 = [
    ("saluti.py", "def saluta(nome):\n    return f'Ciao, {nome}!'\n",
     "test_saluti.py", "from saluti import saluta\n\n\ndef test_saluta():\n    assert saluta('Ada') == 'Ciao, Ada!'\n"),
    ("somme.py", "def somma(a, b):\n    return a + b\n",
     "test_somme.py", "from somme import somma\n\n\ndef test_somma():\n    assert somma(2, 3) == 5\n"),
    ("inverti.py", "def inverti(s):\n    return s[::-1]\n",
     "test_inverti.py", "from inverti import inverti\n\n\ndef test_inverti():\n    assert inverti('abc') == 'cba'\n"),
]


def politica_piano(p_prematura: float, p_obbedienza: float):
    """Tre punti; dopo il secondo, con ``p_prematura``, dichiara finito tutto."""
    stato: dict[str, Any] = {"prematura": None}

    def decidi(v: Vista, rng: random.Random) -> Mossa:
        fatti = [a for n, a, e in v.risultati if n == "manage_plan" and a.get("action") == "complete"
                 and "error" not in e]
        if _indice(v, "manage_plan", lambda a, _e: a.get("action") == "set") < 0:
            return Mossa([("manage_plan", {"action": "set", "steps": [
                "esegui: saluti.py con il suo test", "esegui: somme.py con il suo test",
                "esegui: inverti.py con il suo test"]})], pensiero=800)
        if len(fatti) >= 3:
            return Mossa(testo="Fatto: tre moduli con i loro test.\nVerifica: pytest verde.")
        if len(fatti) == 2:
            if stato["prematura"] is None:
                stato["prematura"] = rng.random() < p_prematura
            if stato["prematura"]:
                if v.sollecito and rng.random() < p_obbedienza:
                    stato["prematura"] = False
                else:
                    return Mossa(testo="Fatto: ho creato i moduli e i test, tutto verde.")
        mod, corpo, test, corpo_test = MODULI_S4[len(fatti)]
        scritto = _indice(v, "write_file", lambda a, e: a.get("filepath") == mod and e.get("status") == "ok")
        if scritto < 0:
            return Mossa([("write_file", {"filepath": mod, "content": corpo}),
                          ("write_file", {"filepath": test, "content": corpo_test})], pensiero=700)
        if _indice(v, "run_command") < scritto:
            return Mossa([("run_command", {"command": PYTEST})])
        return Mossa([("manage_plan", {"action": "complete", "note": f"{mod} verde"})])
    return decidi


# --- S5: dichiarazione senza prova (F8) --------------------------------------

def politica_millanta(p_millanta: float, p_obbedienza: float):
    """Legge il file e poi, con ``p_millanta``, dichiara la modifica senza farla."""
    stato: dict[str, Any] = {"millanta": None}

    def decidi(v: Vista, rng: random.Random) -> Mossa:
        if _indice(v, "read_file") < 0:
            return Mossa([("read_file", {"filepath": "stats.py"})])
        modificato = _indice(v, "edit_file", lambda _a, e: e.get("status") == "ok")
        if modificato >= 0:
            if _indice(v, "run_command") < modificato:
                return Mossa([("run_command", {"command": "python -c \"from stats import Statistiche as S; "
                                                          "assert S([1, 5]).massimo() == 5\""})])
            return Mossa(testo="Fatto: aggiunto massimo() a stats.py.")
        if stato["millanta"] is None:
            stato["millanta"] = rng.random() < p_millanta
        if stato["millanta"]:
            if v.sollecito and rng.random() < p_obbedienza:
                stato["millanta"] = False
            else:
                return Mossa(testo="Fatto: ho aggiunto il metodo massimo() a stats.py.")
        return Mossa([("edit_file", {
            "filepath": "stats.py",
            "old_string": "    def media(self):",
            "new_string": "    def massimo(self):\n        return max(self.valori) if self.valori else None\n\n"
                          "    def media(self):",
        })])
    return decidi


# --- S7: "continua" dopo un turno esaurito, con un rosso aperto (F1, A4) -----

STATS2_PY = STATS_PY + '''
    def mediana(self):
        v = sorted(self.valori)
        return v[len(v) // 2 + 1]
'''
TEST_STATS2 = TEST_STATS + '''

def test_mediana():
    assert Statistiche([1, 2, 3]).mediana() == 2
'''


def politica_ripresa(p_millanta: float, p_obbedienza: float):
    """Primo turno: si ferma per i passi con un rosso aperto. Secondo ("continua"): puo' millantare."""
    stato: dict[str, Any] = {"millanta": None}

    def decidi(v: Vista, rng: random.Random) -> Mossa:
        secondo = any(m.get("role") == "user" and str(m.get("content")).strip() == "continua"
                      for m in v.messaggi)
        mediana = _indice(v, "edit_file", lambda a, e: e.get("status") == "ok"
                          and a.get("new_string", "").endswith("// 2]"))
        if secondo:
            if mediana < 0:
                if stato["millanta"] is None:
                    stato["millanta"] = rng.random() < p_millanta
                if stato["millanta"]:
                    if v.sollecito and rng.random() < p_obbedienza:
                        stato["millanta"] = False
                    else:
                        return Mossa(testo="Ho finito: i test ora passano.")
                return Mossa([("edit_file", {"filepath": "stats.py",
                                             "old_string": "        return v[len(v) // 2 + 1]",
                                             "new_string": "        return v[len(v) // 2]"})])
            if _indice(v, "run_command") < mediana:
                return Mossa([("run_command", {"command": PYTEST})])
            if _verde(v):
                return Mossa(testo="Fatto: corrette media e mediana.\nVerifica: pytest verde.")
            return Mossa(testo="Restano test rossi.")
        # Primo turno, lento: finisce i passi con la mediana ancora rossa.
        if _indice(v, "read_file") < 0:
            return Mossa([("read_file", {"filepath": "stats.py"})], pensiero=900)
        if _indice(v, "run_command") < 0:
            return Mossa([("run_command", {"command": PYTEST})], pensiero=600)
        if _indice(v, "edit_file", lambda _a, e: e.get("status") == "ok") < 0:
            return Mossa([("edit_file", {"filepath": "stats.py", "old_string": RIGA_BUG,
                                         "new_string": RIGA_OK})], pensiero=900)
        return Mossa([("run_command", {"command": PYTEST})], pensiero=1500)
    return decidi


def _ripresa_ok(ws: Path) -> bool:
    testo = (ws / "stats.py").read_text(encoding="utf-8")
    return RIGA_OK in testo and "return v[len(v) // 2]" in testo


# --- S8: pensiero lunghissimo al primo passo (F5, regressione) ---------------

def politica_pensiero(p_obbedienza: float):
    base = politica_modifica(0.0, p_obbedienza, 0.0)

    def decidi(v: Vista, rng: random.Random) -> Mossa:
        if v.sue_mosse == 0 and not v.sollecito:
            return Mossa(pensiero=40_000)
        return base(v, rng)
    return decidi


def scenari(obbedienza: float, diagnostica: float) -> list[Scenario]:
    o = obbedienza
    esplora_file = {f"m{i}.py": f"def f{i}(x):\n    return x + {i}\n" for i in range(1, N_MODULI)}
    esplora_file["m12.py"] = "def f12(x):\n    return x - 1\n"
    esplora_file["test_finale.py"] = "from m12 import f12\n\n\ndef test_f12():\n    assert f12(1) == 2\n"
    return [
        Scenario("S1_modifica_deriva", "F3", "Nel file stats.py la media e' sbagliata: correggila e "
                 f"verifica con {PYTEST}.",
                 {"stats.py": STATS_PY, "test_stats.py": TEST_STATS},
                 lambda: politica_modifica(0.4, o, diagnostica), _fisso_stats),
        Scenario("S2_canale_testo", "F6", "Nel file stats.py la media e' sbagliata: correggila e "
                 f"verifica con {PYTEST}.",
                 {"stats.py": STATS_PY, "test_stats.py": TEST_STATS},
                 lambda: politica_canale(0.35, o), _fisso_stats),
        Scenario("S3_stallo_errante", "F7/F1", f"Fai passare i test ({PYTEST}).",
                 {"app.py": APP_FANTASMA, "test_app.py": TEST_FANTASMA},
                 lambda: politica_stallo(o), lambda _ws: True, max_passi=40),
        Scenario("S3b_esplorazione_lunga", "F2 / falsi positivi A3",
                 f"C'e' un bug che fa fallire test_finale.py: trovalo e correggilo, poi {PYTEST}.",
                 esplora_file, lambda: politica_esplora(o),
                 lambda ws: "return x + 1" in (ws / "m12.py").read_text(encoding="utf-8"), max_passi=30),
        Scenario("S4_piano_aperto", "F8",
                 "Crea tre moduli, ognuno con il suo test:\n1. saluti.py con saluta(nome) che ritorna "
                 "'Ciao, nome!'\n2. somme.py con somma(a, b)\n3. inverti.py con inverti(s) che ritorna la "
                 f"stringa al contrario.\nVerifica ogni modulo con {PYTEST} prima di passare al successivo.",
                 {}, lambda: politica_piano(0.5, o),
                 lambda ws: (ws / "inverti.py").exists() and (ws / "test_inverti.py").exists()),
        Scenario("S5_senza_prova", "F8", "Aggiungi a stats.py un metodo massimo() che ritorna il valore "
                 "piu' grande della lista, None se e' vuota.",
                 {"stats.py": STATS_PY, "test_stats.py": TEST_STATS},
                 lambda: politica_millanta(0.5, o),
                 lambda ws: "def massimo" in (ws / "stats.py").read_text(encoding="utf-8")),
        Scenario("S7_ripresa_con_rosso", "F1/A4", "Correggi stats.py: media e mediana sono sbagliate. "
                 f"Verifica con {PYTEST}.",
                 {"stats.py": STATS2_PY, "test_stats.py": TEST_STATS2},
                 lambda: politica_ripresa(0.5, o), _ripresa_ok, max_passi=5,
                 turni=["continua"], max_passi_turni=[12]),
        Scenario("S8_pensiero_lungo", "F5 (regressione)", "Nel file stats.py la media e' sbagliata: "
                 f"correggila e verifica con {PYTEST}.",
                 {"stats.py": STATS_PY, "test_stats.py": TEST_STATS},
                 lambda: politica_pensiero(o), _fisso_stats),
    ]


# ---------------------------------------------------------------------------
# Esecuzione
# ---------------------------------------------------------------------------


def _kwargs_accettati(fn: Callable[..., Any], kw: dict[str, Any]) -> dict[str, Any]:
    firma = inspect.signature(fn).parameters
    return {k: v for k, v in kw.items() if k in firma}


def esegui_scenario(h: dict[str, Any], sc: Scenario, seme: int) -> dict[str, Any]:
    agent = h["agent"]
    tools = h["tools"]
    config = h["config"]
    prompts = h["prompts"]
    textutils = h["textutils"]
    rng = random.Random(seme * 7919 + zlib.crc32(sc.nome.encode()) % 1000)
    ws = Path(tempfile.mkdtemp(prefix="banco_"))
    try:
        for nome, corpo in sc.file.items():
            (ws / nome).write_text(corpo, encoding="utf-8")
        schema = list(getattr(tools, "TOOLS_SCHEMA_LEAN", tools.TOOLS_SCHEMA))
        schema_tokens = textutils.estimate_tokens(json.dumps(schema, ensure_ascii=False))
        system = prompts.pick_system_prompt(thinking=True)
        env = prompts.build_env_header(str(ws), tool_names=[t["function"]["name"] for t in schema],
                                       sandbox="host")
        politica = sc.politica()
        backend = ModelloFinto(politica, rng, {sc.richiesta.strip(), *[t.strip() for t in sc.turni]},
                               textutils.estimate_messages_tokens, schema_tokens)
        ui: list[dict[str, Any]] = [{"role": "user", "content": sc.richiesta}]
        params = config.GenParams(num_ctx=32768, max_tokens=8192, think=False)
        ctx = tools.ToolContext(workspace=str(ws), sandbox="host", timeout_s=60)
        eventi: Counter = Counter()
        motivi = []
        passi = 0
        nudges: Counter = Counter()
        checkpoint = None
        messaggi_turni = [sc.richiesta, *sc.turni]
        max_turni = [sc.max_passi, *sc.max_passi_turni]
        for t, messaggio in enumerate(messaggi_turni):
            if t > 0:
                ui.append({"role": "user", "content": messaggio})
                ctx = tools.ToolContext(workspace=str(ws), sandbox="host", timeout_s=60,
                                        plan=ctx.plan, notes=ctx.notes, known_files=set(ctx.known_files))
            kw = _kwargs_accettati(agent.run_turn, {
                "backend": backend, "params": params, "tools_schema": schema, "tool_ctx": ctx,
                "ui_messages": ui, "system_prompt": system, "env_header": env,
                "max_steps": max_turni[t], "compact_history": False, "libreria_attiva": False,
                "estratto_pensiero": False, "spec_delega": False, "plan_gate": False,
                "abilita_delega": False, "auto_preview": False, "checkpoint_precedente": checkpoint,
            })
            for ev in agent.run_turn(**kw):
                nome = type(ev).__name__
                eventi[nome] += 1
                if nome == "StepStarted":
                    passi += 1
                if nome == "TurnFinished":
                    motivi.append(ev.reason)
                    nudges.update((ev.usage or {}).get("nudges") or {})
                    checkpoint = getattr(ev, "checkpoint", None)
        fallite = sum(1 for m in ui if m.get("role") == "tool" and m.get("ok") is False)
        chiamate = sum(len(m.get("tool_calls") or []) for m in ui if m.get("role") == "assistant")
        try:
            ok = bool(sc.riuscito(ws))
        except OSError:
            ok = False
        risposta_finale = next((m for m in reversed(ui) if m.get("role") == "assistant"
                                and not m.get("tool_calls")), None)
        # Conta solo il JSON *accettato* come risposta: turno chiuso "completed"
        # con una chiamata scritta come testo. Un turno chiuso con l'errore di
        # protocollo (rete canale esaurita) non ha accettato niente.
        json_finale = bool(risposta_finale and '"arguments"' in str(risposta_finale.get("content"))
                           and motivi and motivi[-1] == "completed")
        return {
            # Riuscito = il criterio sul disco E una chiusura che non sia una
            # chiamata a tool scritta come testo e scambiata per risposta.
            "riuscito": ok and not json_finale,
            "passi": passi,
            "chiamate_modello": backend.chiamate,
            "chiamate_servizio": backend.chiamate_servizio,
            "token_prompt": backend.token_prompt,
            "chiamate_tool": chiamate,
            "tool_falliti": fallite,
            "solleciti": sum(v for k, v in nudges.items()),
            "solleciti_per_tipo": dict(nudges),
            "motivo": motivi[-1] if motivi else "?",
            "motivi": motivi,
            "json_come_risposta": json_finale,
        }
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def riassumi(corse: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(corse)
    motivi = Counter(c["motivo"] for c in corse)
    return {
        "corse": n,
        "riusciti_pct": round(100 * sum(c["riuscito"] for c in corse) / n, 1),
        "passi_media": round(statistics.mean(c["passi"] for c in corse), 2),
        "chiamate_modello_media": round(statistics.mean(c["chiamate_modello"] for c in corse), 2),
        "token_prompt_media": int(statistics.mean(c["token_prompt"] for c in corse)),
        "tool_falliti_media": round(statistics.mean(c["tool_falliti"] for c in corse), 2),
        "tool_falliti_pct": round(100 * sum(c["tool_falliti"] for c in corse)
                                  / max(1, sum(c["chiamate_tool"] for c in corse)), 1),
        "solleciti_media": round(statistics.mean(c["solleciti"] for c in corse), 2),
        "json_come_risposta": sum(c["json_come_risposta"] for c in corse),
        "motivi": dict(motivi),
    }


def prepara_ambiente() -> None:
    """Gli script dei test girano col Python di questo processo, senza bytecode.

    Senza ``PYTHONDONTWRITEBYTECODE`` il banco non era deterministico: fra la
    prima esecuzione dei test e la modifica passano millisecondi, e una riga
    corretta della stessa lunghezza (``x - 1`` -> ``x + 1``) nello stesso
    secondo lascia valido il ``.pyc`` vecchio. Pytest rileggeva il codice di
    prima e il test restava rosso a caso.
    """
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--radice", type=Path, default=Path("."))
    ap.add_argument("--semi", type=int, default=30)
    ap.add_argument("--obbedienza", type=float, default=0.75)
    ap.add_argument("--diagnostica", type=float, default=0.8,
                    help="probabilita' che il modello usi il tratto piu' vicino mostrato dall'errore")
    ap.add_argument("--solo", default="", help="nomi di scenario separati da virgola")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--confronta", nargs=2, type=Path, default=None)
    a = ap.parse_args(argv)
    if a.confronta:
        prima, dopo = (json.loads(p.read_text(encoding="utf-8")) for p in a.confronta)
        for nome in prima["scenari"]:
            x, y = prima["scenari"][nome], dopo["scenari"].get(nome)
            if not y:
                continue
            print(f"{nome}: riusciti {x['riusciti_pct']} -> {y['riusciti_pct']} | passi "
                  f"{x['passi_media']} -> {y['passi_media']} | token {x['token_prompt_media']} -> "
                  f"{y['token_prompt_media']} | falliti {x['tool_falliti_pct']}% -> {y['tool_falliti_pct']}%")
        return 0
    prepara_ambiente()
    h = carica_harness(a.radice)
    risultati: dict[str, Any] = {"radice": str(a.radice.resolve()), "semi": a.semi,
                                 "obbedienza": a.obbedienza, "scenari": {}, "corse": {}}
    t0 = time.monotonic()
    for sc in scenari(a.obbedienza, a.diagnostica):
        if a.solo and sc.nome not in a.solo.split(","):
            continue
        corse = [esegui_scenario(h, sc, seme) for seme in range(a.semi)]
        risultati["scenari"][sc.nome] = {"guasto": sc.guasto, **riassumi(corse)}
        risultati["corse"][sc.nome] = corse
        r = risultati["scenari"][sc.nome]
        print(f"{sc.nome:26} riusciti {r['riusciti_pct']:5.1f}%  passi {r['passi_media']:5.2f}  "
              f"chiamate {r['chiamate_modello_media']:5.2f}  token {r['token_prompt_media']:7d}  "
              f"falliti {r['tool_falliti_pct']:4.1f}%  solleciti {r['solleciti_media']:4.2f}  "
              f"motivi {r['motivi']}", flush=True)
    risultati["durata_s"] = round(time.monotonic() - t0, 1)
    if a.json:
        a.json.write_text(json.dumps(risultati, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
