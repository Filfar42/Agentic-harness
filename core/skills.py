"""Istruzioni che entrano in contesto solo quando servono.

## Il problema

``SYSTEM_PROMPT_LEAN`` e' passato da poco piu' di duemila token a tremila nel
giro di poche sessioni: una sezione sulle note, una sul raggruppamento delle
chiamate, una sul piano. Ogni sezione e' utile e ogni sezione si paga **ad
ogni singola richiesta**, anche quando non c'entra niente -- e su una finestra
da 32k il prompt di sistema e' gia' l'8% del contesto prima che l'utente abbia
scritto una parola. (Il 31/08/2026 e' sceso a 2.822 togliendo una sezione che
ripeteva quella sopra: il numero cala quando qualcuno guarda, e nel frattempo
cresce da solo. E' il motivo per cui serve un meccanismo, non una potatura.)

Una skill e' un pezzo di istruzioni con dei termini che la richiamano. I
termini stanno sempre in contesto (una riga per skill); il corpo entra solo
quando la richiesta li nomina.

## Perche' le carica l'harness e non il modello

La strada ovvia sarebbe un tool ``use_skill``. E' quella che userei con un
modello grande, ed e' sbagliata qui: in tutte le sessioni misurate di questo
progetto ``manage_notes`` -- un tool che il system prompt raccomanda a chiare
lettere -- e' stato chiamato **zero volte**. Un meccanismo che dipende
dall'iniziativa del modello, su questo modello, non scatterebbe mai.

Quindi il riconoscimento lo fa l'harness sul testo della richiesta, prima che
il turno cominci, e costa zero round-trip. E' la stessa regola che vale per il
resto del progetto: quello che l'harness sa gia' deve essere gratis.

## Dove stanno

Due posti, e valgono insieme:

* ``<harness>/skills/`` -- quelle che valgono sempre, per come lavori tu;
* ``<workspace>/.agente/skills/`` -- quelle del progetto aperto, che viaggiano
  con il repo e finiscono sotto controllo di versione insieme al codice che
  descrivono.

Formato: un file markdown con un'intestazione minima.

    ---
    nome: rilascio
    descrizione: come si taglia una release di questo progetto
    termini: release, rilascio, tag, changelog, versione
    ---
    Il corpo, che entra in contesto solo se la richiesta nomina un termine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Tetti volutamente stretti. Una skill che entra in contesto deve costare meno
# del problema che risolve: se ne servissero tre da tremila token l'una,
# tanto valeva lasciarle nel prompt di sistema.
MAX_SKILL_CHARS = 4_000
MAX_SKILL_ATTIVE = 2
MAX_TOTALE_CHARS = 6_000

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


@dataclass(slots=True)
class Skill:
    nome: str
    descrizione: str
    termini: list[str] = field(default_factory=list)
    corpo: str = ""
    origine: str = ""

    def combacia(self, richiesta: str) -> bool:
        """La richiesta nomina uno dei termini di questa skill?

        Confronto a inizio parola e non per sottostringa: cercando "test" si
        prenderebbe "contesto", e una skill caricata a sproposito costa il
        doppio -- i suoi token piu' l'attenzione che ruba a quelli veri.
        """
        testo = richiesta.lower()
        for grezzo in self.termini:
            termine = grezzo.strip().lower()
            if not termine:
                continue
            # Confine da **entrambi** i lati. Con il solo ``(?<!\w)`` davanti,
            # cercare "test" trovava "testo": il lookbehind controlla che prima
            # non ci sia una lettera, non che dopo la parola finisca. Il
            # docstring qui sopra promette il contrario da sempre.
            if re.search(rf"(?<!\w){re.escape(termine)}(?!\w)", testo):
                return True
        return False


def _leggi(path: Path, origine: str) -> Skill | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _FRONTMATTER.match(raw)
    if not match:
        # Senza intestazione non si sa quando caricarla: una skill che non
        # dichiara i suoi termini sarebbe o sempre in contesto o mai.
        return None
    testa, corpo = match.groups()
    campi: dict[str, str] = {}
    for riga in testa.splitlines():
        if ":" in riga:
            chiave, _, valore = riga.partition(":")
            campi[chiave.strip().lower()] = valore.strip()

    nome = campi.get("nome") or path.stem
    descrizione = campi.get("descrizione", "").strip()
    termini = [t for t in re.split(r"[,;]", campi.get("termini", "")) if t.strip()]
    if not descrizione or not termini:
        return None
    return Skill(
        nome=nome,
        descrizione=descrizione,
        termini=termini,
        corpo=corpo.strip()[:MAX_SKILL_CHARS],
        origine=origine,
    )


def carica(cartelle: list[tuple[Path, str]]) -> list[Skill]:
    """Tutte le skill leggibili, in ordine di nome. Gli errori si ignorano.

    Un file malformato non deve impedire di aprire una conversazione: e'
    materiale scritto a mano, e la modalita' di guasto normale e' un trattino
    di troppo nell'intestazione.

    **L'ordine di ``cartelle`` e' significativo**: a parita' di nome vince
    l'ultima, e chi chiama deve quindi passarle dalla piu' generale alla piu'
    specifica (l'harness, poi il progetto). Era vero anche prima ma stava
    scritto solo in un commento a meta' del ciclo, dove non lo legge chi la
    chiama -- e chi la chiama e' l'unico che puo' sbagliarlo.
    """
    trovate: dict[str, Skill] = {}
    for cartella, origine in cartelle:
        try:
            if not cartella.is_dir():
                continue
            files = sorted(cartella.glob("*.md"))
        except OSError:
            continue
        for path in files:
            skill = _leggi(path, origine)
            if skill is not None:
                # Il progetto vince sull'harness: una convenzione locale che
                # contraddice quella generale e' quasi sempre voluta.
                trovate[skill.nome] = skill
    return sorted(trovate.values(), key=lambda s: s.nome)


def scegli(skills: list[Skill], richiesta: str) -> list[Skill]:
    """Le skill che la richiesta richiama, entro i tetti."""
    if not richiesta:
        return []
    scelte: list[Skill] = []
    speso = 0
    for skill in skills:
        if not skill.combacia(richiesta):
            continue
        if len(scelte) >= MAX_SKILL_ATTIVE:
            break
        if speso + len(skill.corpo) > MAX_TOTALE_CHARS:
            # ``continue`` e non ``break``: una skill lunga che non ci sta non
            # deve impedire a quelle corte dopo di lei di entrare. Con ``break``
            # bastava un file grosso in mezzo all'elenco per far sparire tutte
            # le procedure successive, senza che niente lo dicesse.
            continue
        scelte.append(skill)
        speso += len(skill.corpo)
    return scelte


def render_blocco(attive: list[Skill]) -> str:
    """Il blocco da mettere in coda al contesto.

    Entra in contesto SOLO se ci sono skill attive, sollecitate dai termini
    della richiesta dell'utente. Se nessuna combacia il blocco e' vuoto: sul
    modello piccolo l'elenco delle procedure *non* caricate -- che questo
    harness metteva in coda "perche' costa pochi token" -- si e' rivelato
    rumore che gli entra nel ragionamento.

    In coda e non nell'``<environment>`` per la solita ragione: il prefisso
    (system + environment) resta byte-identico fra un passo e l'altro ed e'
    cio' che rende riusabile il KV cache di Ollama; le skill attive cambiano
    da una richiesta all'altra.

    Prende solo le attive: il secondo parametro (l'elenco completo) serviva
    alle dormienti ed era rimasto in firma senza piu' un lettore.
    """
    if not attive:
        return ""
    righe: list[str] = ["<istruzioni_per_questo_compito>"]
    for skill in attive:
        righe += [f"## {skill.nome} — {skill.descrizione}", skill.corpo, ""]
    righe.append("</istruzioni_per_questo_compito>")
    return "\n".join(righe).strip()
