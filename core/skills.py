"""Istruzioni che entrano in contesto solo quando servono.

## Il problema

``SYSTEM_PROMPT_LEAN`` e' passato da poco piu' di duemila token a quasi
duemilasettecento nel giro di poche sessioni: una sezione sulle note, una sul
raggruppamento delle chiamate, una sul piano. Ogni sezione e' utile e ogni
sezione si paga **ad ogni singola richiesta**, anche quando non c'entra niente
-- e su una finestra da 32k il prompt di sistema e' gia' l'8% del contesto
prima che l'utente abbia scritto una parola.

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
        for termine in self.termini:
            termine = termine.strip().lower()
            if not termine:
                continue
            if re.search(rf"(?<!\w){re.escape(termine)}", testo):
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
        if len(scelte) >= MAX_SKILL_ATTIVE or speso + len(skill.corpo) > MAX_TOTALE_CHARS:
            break
        scelte.append(skill)
        speso += len(skill.corpo)
    return scelte


def render_blocco(attive: list[Skill], tutte: list[Skill]) -> str:
    """Il blocco da mettere in coda al contesto.

    In coda e non nell'``<environment>`` per la solita ragione: il prefisso
    (system + environment) e' byte-identico fra un passo e l'altro ed e' cio'
    che rende riusabile il KV cache di Ollama. Le skill attive cambiano da una
    richiesta all'altra.

    L'elenco di quelle *non* caricate ci sta lo stesso, una riga a testa: costa
    pochi token e serve a far dire al modello "per quello che chiedi c'e' una
    procedura, dimmi se la vuoi" invece di improvvisare.
    """
    if not tutte:
        return ""
    righe: list[str] = []
    if attive:
        righe.append("<istruzioni_per_questo_compito>")
        for skill in attive:
            righe += [f"## {skill.nome} — {skill.descrizione}", skill.corpo, ""]
        righe.append("</istruzioni_per_questo_compito>")
    dormienti = [s for s in tutte if s not in attive]
    if dormienti:
        righe.append("")
        righe.append(
            "Altre procedure disponibili, non caricate adesso: "
            + "; ".join(f"{s.nome} ({s.descrizione})" for s in dormienti)
            + ". Se una di queste riguarda quello che stai per fare, dillo "
            "invece di improvvisare."
        )
    return "\n".join(righe).strip()
