"""Package the current complete checkout without dependencies or runtime data."""

import hashlib
import json
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts' / 'Astra-2.38.0-20260915.zip'
SOURCE_DIRS = ('core', 'server', 'web', 'web_mobile', 'tests', 'skills', 'scripts', 'docs')
TOP_FILES = ('run.py', 'run_mobile.py', 'README.md', 'requirements.txt',
             'pyproject.toml', 'uv.lock', 'Dockerfile.sandbox')
EXCLUDE_DIRS = {'__pycache__', '.pytest_cache', '.ruff_cache', 'node_modules', '.venv', '.git'}
# Le evidenze entrano solo se descrivono **questo** sorgente. Gli screenshot
# mascot-*.png e il report mascot-visual-qa.json sono del 14 settembre mattina,
# cioe' di una mascotte che aveva ancora la bocca e stava dentro una fascia
# riservata: allegarli come prova di un pacchetto in cui nessuna delle due cose
# e' piu' vera sarebbe una prova falsa, che e' peggio di una prova mancante. Lo
# script di verifica c'e' -- aggiornato -- e si rilancia.
EVIDENCE = (
    'context-final-integration-tests.txt', 'context-full-tests-final.txt',
    'context-lint-final.txt', 'context-fixes-20260914-manifest.json',
    'context-fixes-20260914.patch', 'mascot-visual-qa.cjs',
)

paths = [ROOT / name for name in TOP_FILES]
for folder in SOURCE_DIRS:
    for current, dirs, files in os.walk(ROOT / folder):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDE_DIRS)
        for name in sorted(files):
            path = Path(current) / name
            if path.suffix.lower() not in {'.pyc', '.pyo', '.zip'}:
                paths.append(path)
paths.extend(ROOT / 'artifacts' / name for name in EVIDENCE)
paths.sort(key=lambda p: p.relative_to(ROOT).as_posix())

release_note = '''# Consegna — 15 settembre 2026

Sorgente completo di **Astra / Local Agent Harness 2.38.0**. Rispetto alla
consegna del 14 settembre cambia il contratto fra il piano di lavoro e le
verifiche, e la mascotte torna alla geometria del marchio.

## Piano e verifiche sono due stati distinti

Il piano diceva a che punto era il lavoro **e** com'era andata la qualita'.
`manage_plan complete` rifiutava qualunque punto finche' esisteva un comando
rosso, e in un lavoro in TDD il rosso iniziale e' il risultato atteso di
"riprodurre il bug": quel punto non si poteva chiudere, il successivo non si
poteva aprire (se ne apre uno per volta), e il modello rilanciava lo stesso
comando finche' i passi non finivano.

Adesso il punto si chiude e la verifica resta rossa, in un registro separato
(`core/verifiche.py`):

- l'**identita'** di una verifica e' il comando normalizzato, non la stringa
  scritta: `pytest x -q` e `python -m pytest x -q` erano due verifiche, e la
  prima restava rossa per sempre;
- l'**ambito** distingue la suite intera da una selezione, cosi' un
  sottoinsieme verde non assolve una suite rossa;
- si **giustifica una verifica per volta** con il motivo scritto: il vecchio
  `clear()` svuotava tutto, e ignorarne una ne faceva sparire due;
- non ogni comando che finisce male e' una verifica: un `rg` senza
  corrispondenze esce 1 ed e' una risposta, `pytest` esce 5 quando non ha
  raccolto niente.

`TurnFinished` porta `qualita`: `reason` dice come si e' fermato il turno, non
se il lavoro e' riuscito. Lo stato compare nell'interfaccia a fine turno, nel
record di sessione e nel riepilogo, che adesso elenca le verifiche per nome.

## Solleciti e stallo

`VERIFY_NUDGE` non ordina piu' di rieseguire il comando identico senza aver
cambiato niente, e dice che un rosso atteso non blocca il piano. Nuovo
`STALLO_NUDGE`: le chiamate **fallite** non entravano affatto nel conteggio
delle ripetizioni, quindi tre `manage_plan` rifiutati di fila non li vedeva
nessuna difesa. Una modifica riuscita azzera il conteggio.

## manage_plan senza step_id

`complete` e `skip` senza `step_id` agiscono sul punto in corso; `start` vale
"riprendi da dove sei". Piano vuoto, piano tutto chiuso e piu' punti aperti
senza nessuno in corso restano tre errori distinti: quando l'intenzione non e'
univoca non si indovina. Il risultato porta `punto_chiuso` con id e testo.

## Mascotte

Niente bocca, in nessuno stato: il simbolo del marchio ha testa, occhi e
antenna, e una bocca disegnata solo nella versione animata faceva due robot
diversi proprio dove le due copie stanno vicine. L'espressione la portano gli
occhi. La fascia nel composer e' uscita dal flusso: prima erano 52 px piu' 4 di
margine che allargavano il riquadro di scrittura, adesso la mascotte gli poggia
sopra, centrata sul tasto di invio. L'ombra sotto i piedi passa dal 15% al 30%.

## Verifiche di questa consegna

- Suite completa **senza Docker** e senza `test_server.py` (che nell'ambiente
  di verifica si blocca sulla sonda vision verso un Ollama inesistente):
  **1.270 passati, 1 saltato, 0 falliti**.
- `ruff check core server tests`: pulito.
- `node --check` su `web/app.js`, `web/companion.js` e
  `artifacts/mascot-visual-qa.cjs`: pulito.
- Geometria della mascotte misurata su un montaggio del `companion.css` e del
  `companion.js` reali: composer alto 51,8 px (prima ~108), centro della
  mascotte e centro del tasto invio coincidenti, piedi 4 px sotto il bordo.

**Non ancora eseguite**: la suite completa con Docker acceso e la verifica
`artifacts/mascot-visual-qa.cjs` nel browser, che richiede Edge e
`scripts/visual_preview.py` attivo. Gli screenshot precedenti non sono allegati
perche' ritraggono la mascotte di ieri.

## Avvio

Estrarre la cartella Astra e seguire README.md. I comandi base sono:

    uv sync --locked --extra dev
    uv run python run.py

Per provare gli stati grafici con dati simulati:

    uv run python scripts/visual_preview.py

Aprire http://127.0.0.1:8137/brand/index.html.

L'archivio include sorgenti, asset, test, skill, documentazione e file delle
dipendenze. Ambienti virtuali, cache, modelli e dati runtime non sono inclusi.
MANIFEST-SHA256.json elenca dimensioni e hash dei file consegnati; la verifica
del pacchetto confronta ogni voce con questi byte e controlla il CRC ZIP.
'''

records = {}
with ZipFile(OUT, 'x', compression=ZIP_DEFLATED, compresslevel=9) as bundle:
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        data = path.read_bytes()
        info = ZipInfo.from_file(path, arcname='Astra/' + relative)
        info.compress_type = ZIP_DEFLATED
        bundle.writestr(info, data)
        records[relative] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    note = release_note.encode('utf-8')
    bundle.writestr('Astra/CONSEGNA.md', note)
    records['CONSEGNA.md'] = {'bytes': len(note), 'sha256': hashlib.sha256(note).hexdigest()}
    manifest = json.dumps(records, ensure_ascii=False, indent=2).encode('utf-8')
    bundle.writestr('Astra/MANIFEST-SHA256.json', manifest)

with ZipFile(OUT) as bundle:
    assert bundle.testzip() is None, 'CRC mismatch'
    names = bundle.namelist()
    assert len(names) == len(set(names)) == len(records) + 1, 'Incorrect archive entries'
    assert set(names) == {'Astra/' + name for name in records} | {'Astra/MANIFEST-SHA256.json'}
    for relative, entry in records.items():
        data = bundle.read('Astra/' + relative)
        assert len(data) == entry['bytes'], relative
        assert hashlib.sha256(data).hexdigest() == entry['sha256'], relative
    for path in paths:
        assert bundle.read('Astra/' + path.relative_to(ROOT).as_posix()) == path.read_bytes(), path
    assert 'mounts: [\'#sidebar .logo\', \'#composer-companion\']' in bundle.read('Astra/web/app.js').decode()
    companion = bundle.read('Astra/web/companion.js').decode()
    assert 'const GESTURE_MS = 1400;' in companion
    # Le tre cose che distinguono questo pacchetto da quello di ieri, verificate
    # sui byte dell'archivio e non sull'albero di lavoro: un pacchetto si
    # controlla per quello che contiene.
    assert 'MOUTHS' not in companion and '__mouth' not in companion, 'la bocca e\' tornata'
    assert 'position: absolute' in bundle.read('Astra/web/companion.css').decode()
    registro = bundle.read('Astra/core/verifiche.py').decode()
    assert 'class RegistroVerifiche' in registro
    strumenti = bundle.read('Astra/core/tools.py').decode()
    assert '_punto_sottinteso' in strumenti
    assert 'def quality_summary' in strumenti

sha = hashlib.sha256(OUT.read_bytes()).hexdigest()
OUT.with_suffix('.zip.sha256').write_text(f'{sha}  {OUT.name}\n', encoding='ascii')
print(json.dumps({'archive': str(OUT), 'bytes': OUT.stat().st_size,
                  'entries': len(records) + 1, 'sha256': sha,
                  'verified': 'CRC and SHA-256 of every entry; exact source equality'}, indent=2))
