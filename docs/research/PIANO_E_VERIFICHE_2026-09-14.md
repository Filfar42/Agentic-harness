# Separare l'avanzamento del piano dallo stato delle verifiche

Revisione del 14 settembre 2026 in `D:\Astra`. Toglie il blocco che impediva di
chiudere un punto del piano con una verifica rossa, e mette al suo posto un
registro delle verifiche che il piano legge e non modifica.

## Il caso

In un lavoro di debug o TDD la sequenza legittima e' *riprodurre → correggere →
verificare*, e il primo punto si conclude **con un test che fallisce**. Con il
contratto precedente quel punto non si poteva chiudere: `manage_plan complete`
rifiutava qualunque punto finche' esisteva un comando rosso. E siccome
`core/plan.py` apre un punto per volta, il modello non poteva nemmeno aprire il
successivo. Restava senza mosse legittime, e quello che faceva -- osservato --
era rilanciare lo stesso comando finche' i passi del turno non finivano.

Il difetto non era nel messaggio d'errore. Era nel contratto: "ho completato
questa attivita'" e "la suite e' verde" erano la stessa condizione.

## Cosa e' cambiato

| Area | Prima | Adesso |
|---|---|---|
| Chiusura di un punto | Rifiutata con un rosso aperto | Sempre consentita; il rosso viaggia nel risultato (`qualita`, `avvisi`) |
| Identita' di una verifica | La stringa del comando | Comando normalizzato: via `uv run`/`python -m`, via le opzioni di sola lettura, argomenti in ordine |
| Ambito | Assente | `suite` / `parziale`: un sottoinsieme verde non assolve una suite rossa |
| `ignore_red` | Azzerava tutto il registro | Giustifica **una** verifica, col motivo attaccato |
| `skip` | Azzerava tutto il registro | Non tocca il registro |
| Comandi non diagnostici | Qualunque exit code ≠ 0 apriva una verifica | `rg`, `grep`, `git`, `ls`… non aprono verifiche; `pytest` 4/5 e shell 126/127 sono comandi sbagliati, non rossi |
| Fine del turno | `reason="completed"` anche con due rossi aperti | `TurnFinished.qualita` dice cosa resta rosso, giustificato o verde |
| Chiamate rifiutate | Non contate da nessuna difesa | `STALLO_NUDGE` alla terza chiamata identica fallita, azzerato da una modifica riuscita |

## Le quattro misure che hanno deciso il disegno

1. **`pytest x -q` rosso e `python -m pytest x -q` verde.** Erano due verifiche.
   La prima restava rossa per sempre su un comando che nessuno avrebbe
   rilanciato, e i solleciti continuavano a nominarla.
2. **Ignorarne una ne spegneva due.** `ToolContext.clear_red_command` chiamava
   `VerificationTracker.clear()`, che svuotava l'intero dizionario. Il secondo
   rosso non lo aveva letto nessuno.
3. **Un `rg` senza corrispondenze diventava una verifica pendente.** Exit 1 e'
   una risposta, non un guasto, e il sollecito chiedeva di rileggere uno stderr
   che non c'era.
4. **Tre `manage_plan` rifiutati di fila non li vedeva nessuna difesa.**
   `RipetizioniTool.registra` veniva chiamata solo dentro un `if ok:`, col
   ragionamento -- corretto in generale -- che rifare una chiamata fallita e'
   legittimo. Lo e' due volte; alla terza e' lo stallo.

## Il registro (`core/verifiche.py`)

Una verifica ha identita', ambito, stato e tentativi. Gli stati sono quattro:

- `verde` — l'ultima esecuzione e' passata;
- `rossa` — l'ultima esecuzione e' fallita ed e' ancora aperta;
- `giustificata` — rossa, ma l'agente ha scritto perche' non riguarda il codice
  del progetto. Non ridiventa rossa da sola: il motivo vale finche' non viene
  ritirato, altrimenti rilanciarla per curiosita' riaprirebbe un blocco gia'
  chiuso e il modello imparerebbe a non rilanciarla piu';
- `sbagliata` — il comando non esiste, le opzioni erano sbagliate, nessun test
  raccolto. Non dice niente sul progetto, ma resta scritto: un turno in cui tre
  verifiche su quattro erano comandi sbagliati non e' un turno andato bene.

Il registro vive nel ciclo agentico, che e' l'unico posto che vede passare i
risultati dei tool. I tool lo leggono attraverso `ToolContext.quality_summary()`
e l'unica scrittura che arriva da fuori e' `giustifica_verifica`, che non
cancella niente.

## Cosa non e' stato fatto, e perche'

**Confronto con una baseline.** Distinguere i fallimenti preesistenti dalle
regressioni nuove richiede esecuzioni confrontabili e il confronto va fatto
sugli **identificativi** dei test, non sul conteggio: da due fallimenti a due
fallimenti puo' nascondere una regressione diversa. Aggiungerlo sopra il blocco
di prima avrebbe lasciato intatta la confusione fra avanzamento e qualita', che
era il difetto vero; adesso che la separazione c'e', la baseline puo' entrare
come lettura in piu' senza toccare il contratto.

**Criteri espliciti per punto** (riproduzione / diagnosi / correzione /
validazione). Reggerebbero meglio di qualunque euristica, ma vanno scritti dal
modello punto per punto, e una modalita' generica "TDD" sarebbe solo un permesso
piu' largo. Da provare dopo, sui piani veri.

## Verifica

Suite eseguita su Linux (Python 3.12) escludendo i file che richiedono Docker
(`test_sandbox`, `test_anteprima`, `test_schermo_e_terminale`, `test_audit_runtime`)
e `test_server.py`, che in quell'ambiente si blocca sulla sonda vision verso un
Ollama inesistente: **1178 passati, 7 saltati, 0 falliti**. In una seconda
esecuzione `test_server.py` e' arrivato a 1038 test passati e 0 falliti prima di
quel blocco. `ruff check core server tests`: pulito. `node --check web/app.js`:
pulito. La suite completa con Docker acceso va rieseguita sulla postazione
Windows, che e' l'ambiente in cui i 27 rossi noti da Docker spento si
distinguono dai rossi veri.
