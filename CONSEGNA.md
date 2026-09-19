# Consegna - 19 settembre 2026: template severi (Astra 2.38.1)

Correzione per il GGUF ufficiale di Qwen3.8
(`ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-IQ3_S-mtp`) su llama-server: ogni messaggio
falliva con due eccezioni Jinja del chat template, e l'errore nella UI
spariva un istante dopo. Dettagli nel README, voce v2.38.1.

- livello di pensiero: i valori ammessi si leggono dal template di `/props`
  (ripiego: il messaggio del 500, imparato e riprovato una volta); traduzione
  sul piu' vicino, a pari distanza il piu' alto (`high` -> `xhigh`); traccia
  `inviato` / `traduzione_livello` sul messaggio assistant;
- system: un solo system in testa al confine di serializzazione
  (`messaggi_per_il_filo`, tutti i transport); cronologia su disco invariata;
- errore: resta in chat (record `error` nella cronologia, riquadro non piu'
  rimosso a fine stream), motivo del server senza involucro JSON, niente
  backoff sui 500 del template.

File: core/backend.py, core/agent.py, core/config.py, web/app.js,
web_mobile/app.js, web_mobile/style.css, tests/test_llamacpp.py,
tests/test_template_severi.py (nuovo), pyproject.toml, uv.lock, README.md.

Verifiche eseguite da Linux (Python 3.12, venv fuori dalla cartella), senza
Docker e senza test_server, test_sandbox, test_anteprima,
test_schermo_e_terminale, test_audit_runtime:
prima 1.212 passati, 7 saltati; dopo 1.241 passati, 7 saltati, 0 falliti.
ruff pulito su core, tests, scripts, server.

Non eseguite: la suite con Docker su Windows, e una prova contro il
llama-server vero. Da fare prima di fidarsi:

    uv run pytest tests -q
    # poi un messaggio qualunque dalla UI, transport llamacpp, e nella
    # sessione salvata la traccia think: "usato": "high", "inviato": "xhigh"

Nota: MANIFEST-SHA256.json non e' rigenerato (lo rifa' il costruttore del
pacchetto).

Backup prima delle modifiche: "Agentic harness_v2.38.0_backup-pre-fix-jinja_2026-09-19.zip"
(core, server, tests, web).
