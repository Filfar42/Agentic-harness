# Validazione effettivamente eseguita

Suite completa sul codice applicativo finale: **1195 passed, 7 skipped in 273.20s**.
Ruff: **All checks passed**. Tutti i sorgenti Python consegnati sono inoltre parsati con AST.
Log integrali: `pytest-final.txt` e `ruff-final.txt`.

Comandi usati nella virtualenv del workspace:

```powershell
.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --tb=short
.venv\Scripts\python.exe -m ruff check core server tests run.py run_mobile.py --no-cache
```

La cache pytest è disabilitata perché la directory preesistente aveva permessi incompatibili;
questo non salta test. I test usano directory temporanee e fixture, senza leggere/scrivere
preferenze e conversazioni reali dell'utente.

## Ambiente

- Python: 3.12.9
- Piattaforma: Windows-10-10.0.19045-SP0
- fastapi: 0.141.1
- starlette: 1.3.1
- httpx: 0.28.1
- uvicorn: 0.52.3
- pydantic: 2.13.4
- pytest: 9.1.1
- ruff: 0.16.3
- quickjs: 1.19.4

## I sette skip

- Quattro prove richiedono symlink reali: tre nuove e una preesistente; Windows ha restituito WinError 1314.
- Una prova di shutdown richiede process group e SIGINT POSIX reali.
- Una prova richiede l'exit code 127 della shell POSIX, non la semantica di cmd.exe.
- Una prova è riservata all'ambiente headless; questa macchina espone il desktop.

Nessun test JavaScript è saltato per mancanza di QuickJS: la dipendenza dev è installata.
Un pass dei test JS non equivale a una sessione end-to-end in un browser reale.

## Contenuto delle prove nuove

- `test_audit_loop.py`: JSON ambiguo, roundtrip degli escape, allowlist, testo inerte, self-correction,
  id abbinati dopo sospensione, stream chiuso, scratch dei figli, disco pieno e metadata patologici.
- `test_audit_transport.py`: errori HTTP/rete, tentativi e backoff, protocollo incompleto,
  limiti frame, buffer tool, metriche fuori range e diagnostica malformata.
- `test_audit_tools.py`: schema prima degli effetti, concorrenza upload/edit e storage di fatti,
  perimetri path e input eccessivi.
- `test_audit_server.py`: origin/peer/host/token, body limit, replay/cancel, client lenti,
  64 consumer SSE, errore worker, transazione settings e doppio POST.
- `test_audit_runtime.py`: timeout/output subprocess, quoting Windows, sharing violation,
  manifest preview malformato, budget schemi, persistenza prima di done e suo fallimento.

## Cosa questi risultati non provano

Nessun benchmark GPU o prova con un modello Ollama/llama.cpp reale; nessun test del kernel/container
Docker reale; nessun test di rete Internet ostile, perdita alimentazione, disco fisicamente guasto,
soak test di giorni, SLO di produzione o verifica multiutente/multiprocesso. Docker è simulato
con un executable stub e i backend con server o trasporti HTTP di test. Le failure iniziali sono
state corrette o sostituite solo quando il contratto richiesto era intenzionalmente insicuro,
come documentato nell'audit. Non è stata eseguita una scansione CVE delle dipendenze.

L'archivio è verificato con CRC ZIP e confronto SHA-256 di ogni entry con il file consegnato.
