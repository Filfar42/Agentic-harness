# Quanto contesto paghi davvero — misura sulle 61 sessioni

Ricostruito passo per passo con le tue funzioni (`build_api_messages` +
`estimate_messages_tokens`), quindi **dopo** `_prune_tool_call` e la
compattazione dei risultati vecchi: è il contesto che il modello riceve, non
quello che c'è nei file. 47 sessioni utili su 61 (le altre sono troppo corte).
Script: `claude output/misura_contesto.py`, si rilancia con
`python misura_contesto.py <num_ctx>` dalla radice del progetto.

---

## 1. Il problema è reale, ed è nato otto giorni fa

| | |
|---|---|
| picco mediano su tutte le sessioni | **8.059 token** |
| picco massimo | **90.018** (sessione del 21/08, al passo 149) |
| sessioni sopra il 25% di 131k | 8 su 47 |
| massimo su tutto ciò che è **precedente** al 20/08 | 38.905 |

La maggioranza delle sessioni non ha nessun problema di contesto: 8.000 token su
131.072 sono niente. Ma le cinque sessioni sopra i 55.000 token sono **tutte del
21-23 agosto**, e sono anche le più lunghe mai fatte (fino a 678 messaggi, 301
passi). Non stai immaginando il peggioramento: la coda pesante è comparsa questa
settimana.

**Perché la compattazione non è mai scattata su quella da 90k.** `num_ctx` è
**131.072**, non 98.304: 90.018 token sono il 68,7%, sotto `compact_threshold =
0.75`. Non è un difetto, è una soglia tarata su una finestra che nel frattempo è
raddoppiata. Alla finestra attuale la compattazione è **di fatto disattivata** —
per raggiungerla servirebbero 98.000 token di contesto, cioè una sessione
mezza volta più lunga della più lunga che hai mai fatto.

(Le uniche 4 compattazioni mai avvenute stanno tutte in una sessione sola, quella
del 19/08 con `qwen3.5:9b`. La nota in memoria che diceva "0 compattazioni su 42
sessioni" era vera allora ed è scaduta.)

---

## 2. Dove vanno i token

Composizione del contesto all'ultimo passo, sommata su tutte le sessioni:

| voce | quota |
|---|---|
| risultati dei tool | **39,0%** |
| argomenti delle tool call | **26,6%** |
| prefisso (system 2.409 + environment) | 19,4% |
| testo dell'assistente | 9,0% |
| utente + blocco di coda | 6,0% |

Due terzi del contesto sono traffico dei tool. Dentro quel traffico:

| tool | quota dei risultati | |
|---|---|---|
| `read_file` | 36,1% | esplorazione |
| `run_command` | 32,7% | |
| **`manage_plan`** | **13,5%** | |
| `search_files` | 9,7% | esplorazione |
| `edit_file` | 5,0% | |
| `write_file` | 1,2% | |
| **esplorazione pura** (read/search/list) | **46,1%** | |

Due cose saltano fuori da sole, e nessuna delle due è la libreria:

**`manage_plan` si mangia il 13,5% dei token di risultato per rimandare
indietro un piano che è già nel blocco di coda.** Lo stesso piano, due volte, a
ogni passo che lo tocca. Il risultato del tool può essere `{"ok": true}` e basta:
il piano il modello lo rilegge dalla coda, dove c'è comunque. È la correzione più
economica che esista in questa lista — poche righe, nessun rischio.

**L'esplorazione è ancora il 46% dei risultati.** È esattamente ciò che
`delega.py` esiste per togliere dal contesto principale. Quel 46% dice che la
delega non viene chiamata, o non abbastanza: è la seconda cosa da guardare, e non
richiede niente di nuovo — richiede di capire perché un tool che c'è non si usa.
(Stessa forma del problema di `manage_notes`: uno strumento che invita.)

---

## 3. L'84,8% del contesto è roba vecchia

All'ultimo passo, **531.076 token contro 94.922**: l'84,8% di quello che il
modello ha davanti è nato più di cinque passi prima. Misurato sul contesto già
potato, non sulla cronologia grezza.

Questo è il tetto teorico di ciò che una libreria di concetti potrebbe togliere.
Non lo raggiungerà — parte di quel vecchio serve — ma dice che lo spazio c'è, ed
è quasi tutto lo spazio.

---

## 4. Il numero che decide tutto

Il contesto si rispedisce **intero a ogni passo**. Moltiplicando:

| sessione | | |
|---|---|---|
| 22/08 (`c4cc`) | 301 passi × 40.506 mediani | **12.464.013 token** |
| 22/08 (`badb`) | 230 passi × 38.429 | 9.150.468 |
| 21/08 (`5929`) | 149 passi × 64.296 | 8.569.650 |
| 22/08 (`ccfc`) | 188 passi × 41.957 | 7.403.331 |
| 23/08 (`f5eb`) | 225 passi × 31.003 | 7.039.097 |
| **totale, 47 sessioni** | | **59.613.945 token** |

Dodici milioni e mezzo di token di prompt **per un turno solo**.

E qui devo correggere quello che ti ho detto prima. Ti avevo scritto che «un
contesto lungo e stabile costa meno di uno corto che riscrivi», perché il KV
cache rende gratis il prefisso riusato. Quel ragionamento vale **per Ollama in
locale**. Ma `api_base` nelle tue impostazioni è `openrouter.ai`, e le cinque
sessioni pesanti girano tutte su `stealth/ox-alpha`, non su qwen locale.

Su un endpoint remoto senza prompt caching garantito, quei 59,6 milioni di token
li spedisci e li paghi davvero — tutti, a ogni passo, in denaro e in latenza di
rete. **L'obiezione si capovolge: con OpenRouter la libreria non è prematura, è
in ritardo.**

---

## 5. Nell'ordine, cosa farei

1. **Una manopola, zero codice**: alla finestra attuale la compattazione non può
   scattare. O `compact_threshold` scende, o `num_ctx` scende. Finché non si
   tocca, tutto quello che hai costruito per il contesto lungo resta spento.
2. **`manage_plan` che non riecheggia il piano.** 13,5% dei token di risultato,
   recuperabili in mezz'ora, senza rischio.
3. **Capire perché la delega non toglie il 46% di esplorazione** che è lì per
   togliere.
4. **Poi** la libreria, agganciata alla compattazione — vedi
   `proposta-libreria-concetti.md`. Ora ha dei numeri sotto: 84,8% di contesto
   vecchio è lo spazio in cui lavora.

I primi tre punti si fanno prima del quarto perché cambiano il denominatore su
cui il quarto va misurato.
