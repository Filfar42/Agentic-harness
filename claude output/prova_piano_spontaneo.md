# Prova del piano spontaneo

> **Cosa cambia rispetto a `prova_piano.md`.** Lì la richiesta era un elenco
> numerato di dodici voci: il modello doveva riordinarle e raggrupparle, ma che
> ci fosse *della struttura* glielo dicevi tu. Qui la richiesta è un testo
> continuo che descrive un risultato, non delle azioni. Nessun numero, nessun
> "step", nessun "prima… poi", nessun verbo all'imperativo in fila. La
> decomposizione non è nascosta: semplicemente non c'è, e va inventata.
>
> **⚠️ Questa prova è già stata fatta, e da v2.22 non misura più la stessa
> cosa.** Esito: nessun piano, zero chiamate a `manage_plan` in trenta passi.
> La spontaneità non c'è. Da quel risultato è nato l'allargamento di
> `looks_multi_step()`, che adesso conta anche gli **artefatti nominati** — e su
> questo testo ne conta sette, quindi il sollecito **scatta**.
>
> Per rifarla come prova di spontaneità serve spegnere "Pretendi un piano" in
> Impostazioni → Efficienza. Lasciandola accesa resta comunque un test utile,
> ma di un'altra cosa: che il sollecito arrivi anche su una richiesta scritta in
> prosa, dove prima passava inosservata.
>
> Il carattere del compito resta quello: ogni singola azione è banale — quattro
> file di testo, due scriptini, un CSV. La difficoltà è tutta nel capire che
> sono otto cose in fila e che tre di esse dipendono dalle altre.

---

## Prima di iniziare

| | |
|---|---|
| **Harness** | v2.22.0, `C:\Users\filip\Claude\Projects\Agentic harness`. |
| **Conversazione** | Nuova, e workspace pulito o comunque senza una cartella `cantiere/`. |
| **Impostazioni → Generazione** | `max_agent_loops` a **24**. Qui c'è anche un ciclo di correzione da pagare: se `prova.py` fallisce, sistemarlo costa passi. |
| **Impostazioni → Efficienza** | Dipende da cosa vuoi misurare. **Spento**, "Pretendi un piano": si torna a misurare la spontaneità. **Acceso**: si misura che il sollecito arrivi anche su una richiesta in prosa. |

---

## Il prompt

Incolla questo, tale e quale:

```
Nel workspace deve esistere una cartella `cantiere/`. Quando avrai finito
voglio trovarci dentro una piccola pipeline che si regge da sola, e questo è
il risultato che mi aspetto.

Dentro `cantiere/grezzi/` quattro file di testo intitolati a un pianeta —
mercurio, venere, terra, marte — ognuno con un numero di righe diverso dagli
altri e tutti sotto la decina. Il contenuto è indifferente.

`cantiere/misura.py`, chiamato senza argomenti dalla radice del workspace,
guarda dentro quell'archivio e ne ricava `cantiere/misure.csv`: intestazione,
poi una riga per file con il nome e il conteggio delle righe, dalla più corta
alla più lunga.

`cantiere/rapporto.md` racconta quanti file ci sono, quale il più lungo e
quale il più corto, e il totale delle righe. I numeri li voglio presi dal CSV
che hai davvero prodotto, non dalla tua memoria.

Se un file sta oltre il doppio della mediana delle righe, in `cantiere/lunghi/`
ci sarà un omonimo `.txt` con la sua prima riga dentro. Se nessuno ci arriva,
la cartella `lunghi/` non deve esistere affatto.

`cantiere/prova.py` mette alla prova tutto questo — una riga di CSV per file,
l'ordinamento giusto, `lunghi/` coerente con la mediana — e passa senza rossi.
Del suo esito voglio l'output testuale.

`cantiere/LEGGIMI.md` chiude, con due righe su come si rifà tutto da zero.

Niente librerie fuori da quella standard, e niente che finisca fuori da
`cantiere/`.
```

---

## Dove sono le trappole

**La decomposizione.** Il testo nomina sette artefatti ma le cose da fare sono
otto o nove, perché "il CSV" e "il programma che lo produce" sono due lavori
diversi e il secondo va anche eseguito. Un piano onesto raggruppa; un piano
copiato dai paragrafi è già un cattivo segno.

**Tre dipendenze vere, e nessuna dichiarata.** `misura.py` non serve a niente
finché `grezzi/` è vuoto; `rapporto.md` non si può scrivere prima di aver letto
il CSV; l'esistenza stessa di `lunghi/` dipende da una mediana che si conosce
solo dopo aver contato. Chi procede paragrafo per paragrafo sbatte contro tutte
e tre.

**La mediana e il suo doppio.** Con quattro conteggi distinti sotto la decina,
il doppio della mediana quasi sempre non lo supera nessuno — quindi `lunghi/`
di solito **non deve esistere**. Ma dipende dai numeri che sceglie lui: con
1, 2, 3 e 9 la mediana è 2,5 e il 9 la supera eccome. Non c'è una risposta
giusta a priori, c'è solo la coerenza fra i numeri scelti, la cartella e quello
che `prova.py` verifica. È la voce dove un modello che indovina invece di
misurare si tradisce da solo.

**Il divieto di ricordare.** "I numeri presi dal CSV che hai davvero prodotto"
vieta la scorciatoia più comoda: scrivere il rapporto con i conteggi che ha in
testa senza rileggere niente.

**La verifica che può fallire davvero.** `prova.py` lo scrive lui e controlla
il suo stesso lavoro. Se torna rosso, l'harness gli impedisce di chiudere il
punto del piano: lì si vede se sa correggere il codice invece del test. Ed è
qui che si è visto il difetto peggiore — un'asserzione sbagliata scritta da lui
che ha mangiato diciassette passi. Da v2.22 al terzo rosso identico l'harness
gli fa notare che quel file l'ha scritto lui e che l'asserzione va riletta:
questa prova serve anche a vedere se quella spinta basta.

---

## Quando ha finito

Mandami lo ZIP con il JSON della sessione e la cartella `cantiere/`.
