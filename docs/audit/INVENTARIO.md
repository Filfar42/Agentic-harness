# Inventario della consegna

107 file di progetto: 28 modificati, 13 nuovi, 66 invariati. Il manifest contiene dimensioni e SHA-256 prima/dopo.

I livelli di esame sono dichiarati per file. Inventariare o eseguire una suite non equivale a dimostrare assenza di vulnerabilità. Le note di audit sono aggiunte all'archivio oltre a questi file; cache, ambiente virtuale e dati utente sono esclusi. Nessun file sorgente applicativo è sostituito da placeholder.

| File | Stato | Esame e verifiche |
|---|---|---|
| `Dockerfile.sandbox` | invariato | Configurazione/documentazione: inventario, lettura e verifica coerenza con runtime. |
| `README.md` | modificato | Configurazione/documentazione: inventario, lettura e verifica coerenza con runtime. |
| `core/__init__.py` | invariato | Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato. |
| `core/agent.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/atomic.py` | nuovo | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/backend.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/compaction.py` | invariato | Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato. |
| `core/config.py` | invariato | Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato. |
| `core/delega.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/deposito.py` | invariato | Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato. |
| `core/jsonsafe.py` | nuovo | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/libreria.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/memory.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/notes.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/pensiero.py` | invariato | Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato. |
| `core/plan.py` | invariato | Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato. |
| `core/process.py` | nuovo | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/profiles.py` | invariato | Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato. |
| `core/prompts.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/sandbox.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/session.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/settings.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/skills.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/spec_delega.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/system_prompt.py` | nuovo | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/textutils.py` | invariato | Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato. |
| `core/tool_validation.py` | nuovo | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/tools.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/vault.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `core/vault_search.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `pyproject.toml` | invariato | Configurazione/documentazione: inventario, lettura e verifica coerenza con runtime. |
| `requirements.txt` | invariato | Configurazione/documentazione: inventario, lettura e verifica coerenza con runtime. |
| `run.py` | invariato | Configurazione/documentazione: inventario, lettura e verifica coerenza con runtime. |
| `run_mobile.py` | invariato | Configurazione/documentazione: inventario, lettura e verifica coerenza con runtime. |
| `scripts/build_audit_bundle.py` | nuovo | Script di consegna: eseguito, archivio verificato con CRC e hash. |
| `server/__init__.py` | invariato | Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato. |
| `server/main.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `server/mobile.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `server/nativedialog.py` | invariato | Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato. |
| `server/prep.py` | invariato | Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato. |
| `server/previewhost.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `server/runner.py` | modificato | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `server/security.py` | nuovo | Confini modificati e review del diff; compilazione AST, Ruff e regressioni. |
| `skills/esempio-rilascio.md` | invariato | Configurazione/documentazione: inventario, lettura e verifica coerenza con runtime. |
| `tests/conftest.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_agent_loop.py` | modificato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_allegati_e_workspace.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_anteprima.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_audit_loop.py` | nuovo | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_audit_runtime.py` | nuovo | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_audit_server.py` | nuovo | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_audit_tools.py` | nuovo | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_audit_transport.py` | nuovo | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_avvio.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_banco_di_prova.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_compattazione.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_contesto_adattivo.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_core.py` | modificato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_delega.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_deposito.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_estratto_pensiero.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_gocce_file.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_goccia_pensiero.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_guardia_test.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_guardie_lavoro.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_impostazioni_allineate.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_interfaccia_colonne.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_libreria.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_llamacpp.py` | modificato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_mobile_passi.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_mobile_proxy.py` | modificato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_mobile_ui.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_passi_sprecati.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_persistenza_coda.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_piano.py` | modificato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_prestazioni.py` | modificato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_prestiti.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_profiles.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_prontezza.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_reattivita_e_ripresa.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_ricerca_chat.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_ricerca_online.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_riepilogo_e_pensiero.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_sandbox.py` | modificato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_schermo_e_terminale.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_server.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_settings_persistenti.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_spec_delega.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_vault.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_verifica_ostinata.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `tests/test_vram_remota.py` | invariato | Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md. |
| `uv.lock` | nuovo | Configurazione/documentazione: inventario, lettura e verifica coerenza con runtime. |
| `web/app.js` | modificato | Revisione streaming/rendering/confine API e test UI esistenti; nessun benchmark browser completo. |
| `web/favicon-16.png` | invariato | Asset statico: inventario e integrità; non analisi pixel o decodificatore. |
| `web/favicon-32.png` | invariato | Asset statico: inventario e integrità; non analisi pixel o decodificatore. |
| `web/favicon-64.png` | invariato | Asset statico: inventario e integrità; non analisi pixel o decodificatore. |
| `web/index.html` | invariato | Revisione streaming/rendering/confine API e test UI esistenti; nessun benchmark browser completo. |
| `web/logo-dark-64.png` | invariato | Asset statico: inventario e integrità; non analisi pixel o decodificatore. |
| `web/logo-light-64.png` | invariato | Asset statico: inventario e integrità; non analisi pixel o decodificatore. |
| `web/style.css` | invariato | Revisione streaming/rendering/confine API e test UI esistenti; nessun benchmark browser completo. |
| `web_mobile/app.js` | invariato | Revisione streaming/rendering/confine API e test UI esistenti; nessun benchmark browser completo. |
| `web_mobile/icons/apple-touch-icon.png` | invariato | Asset statico: inventario e integrità; non analisi pixel o decodificatore. |
| `web_mobile/icons/icon-192.png` | invariato | Asset statico: inventario e integrità; non analisi pixel o decodificatore. |
| `web_mobile/icons/icon-512.png` | invariato | Asset statico: inventario e integrità; non analisi pixel o decodificatore. |
| `web_mobile/index.html` | invariato | Revisione streaming/rendering/confine API e test UI esistenti; nessun benchmark browser completo. |
| `web_mobile/manifest.webmanifest` | invariato | Revisione streaming/rendering/confine API e test UI esistenti; nessun benchmark browser completo. |
| `web_mobile/style.css` | invariato | Revisione streaming/rendering/confine API e test UI esistenti; nessun benchmark browser completo. |
