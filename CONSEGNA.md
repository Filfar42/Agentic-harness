# Consegna - 15 settembre 2026 (sera): regia del pensiero

Sorgente completo di Astra 2.38.0 con le modifiche della sera del 15/09:

- comunicazione senza ripetizioni (contratto di chiusura Fatto/Verifica/Poi,
  blocco <comunicazione_turno>);
- regia del pensiero (core/regia_pensiero.py): tipo dei punti del piano
  (esegui/indaga/diagnosi/progetta) e budget per tipo e fase, correzioni
  retrospettive, chiusura per continuazione su Ollama con ricaduta sul
  watchdog, rilevatore di oscillazione e di decisione scritta, registro delle
  ipotesi (manage_plan action='ipotesi'), budget di sintesi per le indagini;
- scripts/sonda_continuazione.py e scripts/analisi_regia_pensiero.py.

Verifiche eseguite da Linux, senza Docker e senza test_server.py:
1.210 passati, 7 saltati, 0 falliti; ruff pulito su core, tests, scripts, server.
Non eseguite: suite con Docker, e la sonda contro un Ollama vero -- da lanciare
prima di fidarsi della continuazione:

    .venv\Scripts\python scripts\sonda_continuazione.py --model qwen3.8:27b

Esclusi: ambienti virtuali, cache, sessioni, impostazioni locali
(agent_settings.json contiene chiavi) e modelli.
