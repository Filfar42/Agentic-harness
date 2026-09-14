# Verifica del contrasto — Graphite / Mineral

**189/189 combinazioni conformi alla soglia misurata.**

Il controllo legge i token correnti di `web/style.css` e `web_mobile/style.css`. Misura testo normale, metadati, stati hover/selezionati, azioni premute, tinte composite, opacità esplicite e indicatori di focus. Non assume l'esenzione per testo grande: usa 4.5:1 per tutto il testo, anche quello più piccolo. La soglia è quella di [WCAG 2.1, contrasto minimo](https://www.w3.org/WAI/WCAG21/Understanding/contrast-minimum.html).

Per contorni funzionali, icone significative e focus usa 3:1 rispetto allo sfondo adiacente, secondo [WCAG 2.1, contrasto non testuale](https://www.w3.org/WAI/WCAG21/Understanding/non-text-contrast.html). I separatori decorativi `--border`, i loghi e i controlli disabilitati non sono trattati come contorni funzionali. Gli stati selezionati usano l'accento come indicatore.

È una verifica della palette e delle combinazioni elencate, **non una certificazione WCAG completa**. Non dimostra da sola semantica accessibile, navigazione da tastiera, assenza di sovrapposizioni, comportamento dei componenti nativi, contrasto di contenuti caricati dall'utente o contrasto durante ogni fotogramma delle transizioni. Il testo delle aree scorrevoli non viene piu' sfumato da maschere CSS.

## Riproduzione

```powershell
.venv\Scripts\python.exe scripts/check_visual_contrast.py --write-report
```

Nessuna dipendenza esterna. L'esito restituisce codice 1 se una coppia fallisce. Le tinte vengono composte in sRGB prima del calcolo della luminanza; il confronto usa valori non arrotondati. I rapporti e gli esadecimali mostrati sono arrotondati.

## Minimo per famiglia

| Tema | Famiglia | Peggiore combinazione | Rapporto | Soglia | Esito |
| --- | --- | --- | ---: | ---: | --- |
| Desktop light | Testo --text | --text / --selected-bg | 11.97:1 | 4.5:1 | PASS |
| Desktop light | Testo --text-muted | --text-muted / --selected-bg | 5.12:1 | 4.5:1 | PASS |
| Desktop light | Testo --text-faint | --text-faint / --selected-bg | 4.80:1 | 4.5:1 | PASS |
| Desktop light | Testo --accent | --accent / --selected-bg | 4.81:1 | 4.5:1 | PASS |
| Desktop light | Testo --ok | --ok / --selected-bg | 5.31:1 | 4.5:1 | PASS |
| Desktop light | Testo --warn | --warn / --selected-bg | 4.90:1 | 4.5:1 | PASS |
| Desktop light | Testo --err | --err / --selected-bg | 4.84:1 | 4.5:1 | PASS |
| Desktop light | Azioni primarie | --on-accent / --accent | 5.87:1 | 4.5:1 | PASS |
| Desktop light | Interruzione | --surface / --err | 5.91:1 | 4.5:1 | PASS |
| Desktop light | Contorni controlli | --control-border / --bg-subtle | 3.38:1 | 3:1 | PASS |
| Desktop light | Focus e selezione | --focus-ring / --selected-bg | 4.81:1 | 3:1 | PASS |
| Desktop light | Piano: tinte di stato | --text-faint / --tint-warn su --surface | 5.24:1 | 4.5:1 | PASS |
| Desktop light | Errore: tinta | --err / --err 8% su --surface | 5.25:1 | 4.5:1 | PASS |
| Desktop light | Note inline con opacità | --text-faint (opacity 1) / --bg-subtle | 5.18:1 | 4.5:1 | PASS |
| Desktop light | Chevron espansione | --accent (opacity 1) / --surface-alt | 5.34:1 | 3:1 | PASS |
| Desktop dark | Testo --text | --text / --selected-bg | 9.84:1 | 4.5:1 | PASS |
| Desktop dark | Testo --text-muted | --text-muted / --selected-bg | 6.05:1 | 4.5:1 | PASS |
| Desktop dark | Testo --text-faint | --text-faint / --selected-bg | 5.36:1 | 4.5:1 | PASS |
| Desktop dark | Testo --accent | --accent / --selected-bg | 6.26:1 | 4.5:1 | PASS |
| Desktop dark | Testo --ok | --ok / --selected-bg | 6.33:1 | 4.5:1 | PASS |
| Desktop dark | Testo --warn | --warn / --selected-bg | 6.31:1 | 4.5:1 | PASS |
| Desktop dark | Testo --err | --err / --selected-bg | 5.64:1 | 4.5:1 | PASS |
| Desktop dark | Azioni primarie | --on-accent / --accent | 8.60:1 | 4.5:1 | PASS |
| Desktop dark | Interruzione | --surface / --err | 8.10:1 | 4.5:1 | PASS |
| Desktop dark | Contorni controlli | --control-border / --surface-alt | 3.69:1 | 3:1 | PASS |
| Desktop dark | Focus e selezione | --focus-ring / --selected-bg | 6.26:1 | 3:1 | PASS |
| Desktop dark | Piano: tinte di stato | --text-faint / --tint-warn su --surface | 6.37:1 | 4.5:1 | PASS |
| Desktop dark | Errore: tinta | --err / --err 8% su --surface | 6.97:1 | 4.5:1 | PASS |
| Desktop dark | Note inline con opacità | --text-faint (opacity 1) / --surface | 7.70:1 | 4.5:1 | PASS |
| Desktop dark | Chevron espansione | --text-muted (opacity 1) / --surface-alt | 7.69:1 | 3:1 | PASS |
| Mobile dark | Testo --testo | --testo / --utente | 8.89:1 | 4.5:1 | PASS |
| Mobile dark | Testo --testo-dim | --testo-dim / --utente | 5.13:1 | 4.5:1 | PASS |
| Mobile dark | Azioni primarie | --su-accento / --accento-active | 6.40:1 | 4.5:1 | PASS |
| Mobile dark | Interruzione | Premuto | 6.04:1 | 4.5:1 | PASS |
| Mobile dark | Contorni controlli | --bordo-control / --panel-hover | 3.58:1 | 3:1 | PASS |
| Mobile dark | Focus e selezione | --accento / --panel-hover | 7.68:1 | 3:1 | PASS |
| Mobile dark | Errore: banner | --errore / --errore-bg | 8.56:1 | 4.5:1 | PASS |
| Mobile dark | Passi con opacità | --testo-dim (opacity 0.85) / --bg | 6.97:1 | 4.5:1 | PASS |
| Mobile dark | Codice inline | --testo / .msg code su --utente | 7.13:1 | 4.5:1 | PASS |
| Mobile dark | Domande e risposte | --testo-dim / .msg.pending.risposta-data | 8.26:1 | 4.5:1 | PASS |
| Mobile dark | Badge attività | --ok / badge, opacità minima 0.88 | 6.10:1 | 4.5:1 | PASS |

## Matrice completa

<details>
<summary>Mostra tutte le combinazioni misurate</summary>

| Tema | Combinazione | Testo/segno effettivo | Sfondo effettivo | Rapporto | Soglia | Esito |
| --- | --- | --- | --- | ---: | ---: | --- |
| Desktop light | --text / --bg | #1c2b30 | #f7f9f9 | 13.82:1 | 4.5:1 | PASS |
| Desktop light | --text / --bg-subtle | #1c2b30 | #edf2f2 | 12.93:1 | 4.5:1 | PASS |
| Desktop light | --text / --surface | #1c2b30 | #ffffff | 14.61:1 | 4.5:1 | PASS |
| Desktop light | --text / --surface-alt | #1c2b30 | #f1f5f5 | 13.30:1 | 4.5:1 | PASS |
| Desktop light | --text / --hover-bg | #1c2b30 | #e3ebec | 12.08:1 | 4.5:1 | PASS |
| Desktop light | --text / --selected-bg | #1c2b30 | #dcece8 | 11.97:1 | 4.5:1 | PASS |
| Desktop light | --text / --accent-soft | #1c2b30 | #e3f1ee | 12.58:1 | 4.5:1 | PASS |
| Desktop light | --text / --code-bg | #1c2b30 | #edf2f3 | 12.94:1 | 4.5:1 | PASS |
| Desktop light | --text-muted / --bg | #4f646a | #f7f9f9 | 5.91:1 | 4.5:1 | PASS |
| Desktop light | --text-muted / --bg-subtle | #4f646a | #edf2f2 | 5.53:1 | 4.5:1 | PASS |
| Desktop light | --text-muted / --surface | #4f646a | #ffffff | 6.24:1 | 4.5:1 | PASS |
| Desktop light | --text-muted / --surface-alt | #4f646a | #f1f5f5 | 5.68:1 | 4.5:1 | PASS |
| Desktop light | --text-muted / --hover-bg | #4f646a | #e3ebec | 5.16:1 | 4.5:1 | PASS |
| Desktop light | --text-muted / --selected-bg | #4f646a | #dcece8 | 5.12:1 | 4.5:1 | PASS |
| Desktop light | --text-muted / --accent-soft | #4f646a | #e3f1ee | 5.38:1 | 4.5:1 | PASS |
| Desktop light | --text-muted / --code-bg | #4f646a | #edf2f3 | 5.53:1 | 4.5:1 | PASS |
| Desktop light | --text-faint / --bg | #55686d | #f7f9f9 | 5.54:1 | 4.5:1 | PASS |
| Desktop light | --text-faint / --bg-subtle | #55686d | #edf2f2 | 5.18:1 | 4.5:1 | PASS |
| Desktop light | --text-faint / --surface | #55686d | #ffffff | 5.85:1 | 4.5:1 | PASS |
| Desktop light | --text-faint / --surface-alt | #55686d | #f1f5f5 | 5.33:1 | 4.5:1 | PASS |
| Desktop light | --text-faint / --hover-bg | #55686d | #e3ebec | 4.84:1 | 4.5:1 | PASS |
| Desktop light | --text-faint / --selected-bg | #55686d | #dcece8 | 4.80:1 | 4.5:1 | PASS |
| Desktop light | --text-faint / --accent-soft | #55686d | #e3f1ee | 5.04:1 | 4.5:1 | PASS |
| Desktop light | --text-faint / --code-bg | #55686d | #edf2f3 | 5.18:1 | 4.5:1 | PASS |
| Desktop light | --accent / --bg | #0b7169 | #f7f9f9 | 5.55:1 | 4.5:1 | PASS |
| Desktop light | --accent / --bg-subtle | #0b7169 | #edf2f2 | 5.19:1 | 4.5:1 | PASS |
| Desktop light | --accent / --surface | #0b7169 | #ffffff | 5.87:1 | 4.5:1 | PASS |
| Desktop light | --accent / --surface-alt | #0b7169 | #f1f5f5 | 5.34:1 | 4.5:1 | PASS |
| Desktop light | --accent / --hover-bg | #0b7169 | #e3ebec | 4.85:1 | 4.5:1 | PASS |
| Desktop light | --accent / --selected-bg | #0b7169 | #dcece8 | 4.81:1 | 4.5:1 | PASS |
| Desktop light | --accent / --accent-soft | #0b7169 | #e3f1ee | 5.05:1 | 4.5:1 | PASS |
| Desktop light | --accent / --code-bg | #0b7169 | #edf2f3 | 5.19:1 | 4.5:1 | PASS |
| Desktop light | --ok / --bg | #186b49 | #f7f9f9 | 6.14:1 | 4.5:1 | PASS |
| Desktop light | --ok / --bg-subtle | #186b49 | #edf2f2 | 5.74:1 | 4.5:1 | PASS |
| Desktop light | --ok / --surface | #186b49 | #ffffff | 6.49:1 | 4.5:1 | PASS |
| Desktop light | --ok / --surface-alt | #186b49 | #f1f5f5 | 5.90:1 | 4.5:1 | PASS |
| Desktop light | --ok / --hover-bg | #186b49 | #e3ebec | 5.36:1 | 4.5:1 | PASS |
| Desktop light | --ok / --selected-bg | #186b49 | #dcece8 | 5.31:1 | 4.5:1 | PASS |
| Desktop light | --ok / --accent-soft | #186b49 | #e3f1ee | 5.58:1 | 4.5:1 | PASS |
| Desktop light | --ok / --code-bg | #186b49 | #edf2f3 | 5.74:1 | 4.5:1 | PASS |
| Desktop light | --warn / --bg | #865b0a | #f7f9f9 | 5.65:1 | 4.5:1 | PASS |
| Desktop light | --warn / --bg-subtle | #865b0a | #edf2f2 | 5.29:1 | 4.5:1 | PASS |
| Desktop light | --warn / --surface | #865b0a | #ffffff | 5.98:1 | 4.5:1 | PASS |
| Desktop light | --warn / --surface-alt | #865b0a | #f1f5f5 | 5.44:1 | 4.5:1 | PASS |
| Desktop light | --warn / --hover-bg | #865b0a | #e3ebec | 4.94:1 | 4.5:1 | PASS |
| Desktop light | --warn / --selected-bg | #865b0a | #dcece8 | 4.90:1 | 4.5:1 | PASS |
| Desktop light | --warn / --accent-soft | #865b0a | #e3f1ee | 5.15:1 | 4.5:1 | PASS |
| Desktop light | --warn / --code-bg | #865b0a | #edf2f3 | 5.29:1 | 4.5:1 | PASS |
| Desktop light | --err / --bg | #b03b44 | #f7f9f9 | 5.59:1 | 4.5:1 | PASS |
| Desktop light | --err / --bg-subtle | #b03b44 | #edf2f2 | 5.23:1 | 4.5:1 | PASS |
| Desktop light | --err / --surface | #b03b44 | #ffffff | 5.91:1 | 4.5:1 | PASS |
| Desktop light | --err / --surface-alt | #b03b44 | #f1f5f5 | 5.38:1 | 4.5:1 | PASS |
| Desktop light | --err / --hover-bg | #b03b44 | #e3ebec | 4.88:1 | 4.5:1 | PASS |
| Desktop light | --err / --selected-bg | #b03b44 | #dcece8 | 4.84:1 | 4.5:1 | PASS |
| Desktop light | --err / --accent-soft | #b03b44 | #e3f1ee | 5.09:1 | 4.5:1 | PASS |
| Desktop light | --err / --code-bg | #b03b44 | #edf2f3 | 5.23:1 | 4.5:1 | PASS |
| Desktop light | --on-accent / --accent | #ffffff | #0b7169 | 5.87:1 | 4.5:1 | PASS |
| Desktop light | --on-accent / --accent-hover | #ffffff | #085d57 | 7.74:1 | 4.5:1 | PASS |
| Desktop light | --surface / --text-muted | #ffffff | #4f646a | 6.24:1 | 4.5:1 | PASS |
| Desktop light | --surface / --err | #ffffff | #b03b44 | 5.91:1 | 4.5:1 | PASS |
| Desktop light | --control-border / --bg | #72868a | #f7f9f9 | 3.62:1 | 3:1 | PASS |
| Desktop light | --control-border / --bg-subtle | #72868a | #edf2f2 | 3.38:1 | 3:1 | PASS |
| Desktop light | --control-border / --surface | #72868a | #ffffff | 3.82:1 | 3:1 | PASS |
| Desktop light | --control-border / --surface-alt | #72868a | #f1f5f5 | 3.48:1 | 3:1 | PASS |
| Desktop light | --focus-ring / --bg | #0b7169 | #f7f9f9 | 5.55:1 | 3:1 | PASS |
| Desktop light | --focus-ring / --bg-subtle | #0b7169 | #edf2f2 | 5.19:1 | 3:1 | PASS |
| Desktop light | --focus-ring / --surface | #0b7169 | #ffffff | 5.87:1 | 3:1 | PASS |
| Desktop light | --focus-ring / --surface-alt | #0b7169 | #f1f5f5 | 5.34:1 | 3:1 | PASS |
| Desktop light | --focus-ring / --hover-bg | #0b7169 | #e3ebec | 4.85:1 | 3:1 | PASS |
| Desktop light | --focus-ring / --selected-bg | #0b7169 | #dcece8 | 4.81:1 | 3:1 | PASS |
| Desktop light | --focus-ring / --accent-soft | #0b7169 | #e3f1ee | 5.05:1 | 3:1 | PASS |
| Desktop light | --focus-ring / --code-bg | #0b7169 | #edf2f3 | 5.19:1 | 3:1 | PASS |
| Desktop light | --ok / --tint-ok su --surface | #186b49 | #eff5f2 | 5.86:1 | 4.5:1 | PASS |
| Desktop light | --text-faint / --tint-ok su --surface | #55686d | #eff5f2 | 5.29:1 | 4.5:1 | PASS |
| Desktop light | --warn / --tint-warn su --surface | #865b0a | #f5f2eb | 5.34:1 | 4.5:1 | PASS |
| Desktop light | --text-faint / --tint-warn su --surface | #55686d | #f5f2eb | 5.24:1 | 4.5:1 | PASS |
| Desktop light | --err / --err 8% su --surface | #b03b44 | #f9eff0 | 5.25:1 | 4.5:1 | PASS |
| Desktop light | --text-faint (opacity 1) / --surface | #55686d | #ffffff | 5.85:1 | 4.5:1 | PASS |
| Desktop light | --text-faint (opacity 1) / --bg-subtle | #55686d | #edf2f2 | 5.18:1 | 4.5:1 | PASS |
| Desktop light | --text-muted (opacity 1) / --surface-alt | #4f646a | #f1f5f5 | 5.68:1 | 3:1 | PASS |
| Desktop light | --accent (opacity 1) / --surface-alt | #0b7169 | #f1f5f5 | 5.34:1 | 3:1 | PASS |
| Desktop dark | --text / --bg | #edf3f3 | #11181b | 15.99:1 | 4.5:1 | PASS |
| Desktop dark | --text / --bg-subtle | #edf3f3 | #151e22 | 15.08:1 | 4.5:1 | PASS |
| Desktop dark | --text / --surface | #edf3f3 | #192428 | 14.13:1 | 4.5:1 | PASS |
| Desktop dark | --text / --surface-alt | #edf3f3 | #202e32 | 12.49:1 | 4.5:1 | PASS |
| Desktop dark | --text / --hover-bg | #edf3f3 | #26383d | 10.91:1 | 4.5:1 | PASS |
| Desktop dark | --text / --selected-bg | #edf3f3 | #25413d | 9.84:1 | 4.5:1 | PASS |
| Desktop dark | --text / --accent-soft | #edf3f3 | #1d3734 | 11.35:1 | 4.5:1 | PASS |
| Desktop dark | --text / --code-bg | #edf3f3 | #111b1e | 15.60:1 | 4.5:1 | PASS |
| Desktop dark | --text-muted / --bg | #b2c3c6 | #11181b | 9.84:1 | 4.5:1 | PASS |
| Desktop dark | --text-muted / --bg-subtle | #b2c3c6 | #151e22 | 9.28:1 | 4.5:1 | PASS |
| Desktop dark | --text-muted / --surface | #b2c3c6 | #192428 | 8.69:1 | 4.5:1 | PASS |
| Desktop dark | --text-muted / --surface-alt | #b2c3c6 | #202e32 | 7.69:1 | 4.5:1 | PASS |
| Desktop dark | --text-muted / --hover-bg | #b2c3c6 | #26383d | 6.71:1 | 4.5:1 | PASS |
| Desktop dark | --text-muted / --selected-bg | #b2c3c6 | #25413d | 6.05:1 | 4.5:1 | PASS |
| Desktop dark | --text-muted / --accent-soft | #b2c3c6 | #1d3734 | 6.99:1 | 4.5:1 | PASS |
| Desktop dark | --text-muted / --code-bg | #b2c3c6 | #111b1e | 9.60:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint / --bg | #a5b8bd | #11181b | 8.71:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint / --bg-subtle | #a5b8bd | #151e22 | 8.21:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint / --surface | #a5b8bd | #192428 | 7.70:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint / --surface-alt | #a5b8bd | #202e32 | 6.80:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint / --hover-bg | #a5b8bd | #26383d | 5.94:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint / --selected-bg | #a5b8bd | #25413d | 5.36:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint / --accent-soft | #a5b8bd | #1d3734 | 6.18:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint / --code-bg | #a5b8bd | #111b1e | 8.50:1 | 4.5:1 | PASS |
| Desktop dark | --accent / --bg | #68d5c4 | #11181b | 10.17:1 | 4.5:1 | PASS |
| Desktop dark | --accent / --bg-subtle | #68d5c4 | #151e22 | 9.59:1 | 4.5:1 | PASS |
| Desktop dark | --accent / --surface | #68d5c4 | #192428 | 8.99:1 | 4.5:1 | PASS |
| Desktop dark | --accent / --surface-alt | #68d5c4 | #202e32 | 7.94:1 | 4.5:1 | PASS |
| Desktop dark | --accent / --hover-bg | #68d5c4 | #26383d | 6.94:1 | 4.5:1 | PASS |
| Desktop dark | --accent / --selected-bg | #68d5c4 | #25413d | 6.26:1 | 4.5:1 | PASS |
| Desktop dark | --accent / --accent-soft | #68d5c4 | #1d3734 | 7.22:1 | 4.5:1 | PASS |
| Desktop dark | --accent / --code-bg | #68d5c4 | #111b1e | 9.92:1 | 4.5:1 | PASS |
| Desktop dark | --ok / --bg | #7ad6ad | #11181b | 10.30:1 | 4.5:1 | PASS |
| Desktop dark | --ok / --bg-subtle | #7ad6ad | #151e22 | 9.71:1 | 4.5:1 | PASS |
| Desktop dark | --ok / --surface | #7ad6ad | #192428 | 9.10:1 | 4.5:1 | PASS |
| Desktop dark | --ok / --surface-alt | #7ad6ad | #202e32 | 8.04:1 | 4.5:1 | PASS |
| Desktop dark | --ok / --hover-bg | #7ad6ad | #26383d | 7.02:1 | 4.5:1 | PASS |
| Desktop dark | --ok / --selected-bg | #7ad6ad | #25413d | 6.33:1 | 4.5:1 | PASS |
| Desktop dark | --ok / --accent-soft | #7ad6ad | #1d3734 | 7.31:1 | 4.5:1 | PASS |
| Desktop dark | --ok / --code-bg | #7ad6ad | #111b1e | 10.05:1 | 4.5:1 | PASS |
| Desktop dark | --warn / --bg | #e8be6a | #11181b | 10.26:1 | 4.5:1 | PASS |
| Desktop dark | --warn / --bg-subtle | #e8be6a | #151e22 | 9.68:1 | 4.5:1 | PASS |
| Desktop dark | --warn / --surface | #e8be6a | #192428 | 9.06:1 | 4.5:1 | PASS |
| Desktop dark | --warn / --surface-alt | #e8be6a | #202e32 | 8.01:1 | 4.5:1 | PASS |
| Desktop dark | --warn / --hover-bg | #e8be6a | #26383d | 7.00:1 | 4.5:1 | PASS |
| Desktop dark | --warn / --selected-bg | #e8be6a | #25413d | 6.31:1 | 4.5:1 | PASS |
| Desktop dark | --warn / --accent-soft | #e8be6a | #1d3734 | 7.28:1 | 4.5:1 | PASS |
| Desktop dark | --warn / --code-bg | #e8be6a | #111b1e | 10.01:1 | 4.5:1 | PASS |
| Desktop dark | --err / --bg | #ff9eaa | #11181b | 9.16:1 | 4.5:1 | PASS |
| Desktop dark | --err / --bg-subtle | #ff9eaa | #151e22 | 8.64:1 | 4.5:1 | PASS |
| Desktop dark | --err / --surface | #ff9eaa | #192428 | 8.10:1 | 4.5:1 | PASS |
| Desktop dark | --err / --surface-alt | #ff9eaa | #202e32 | 7.16:1 | 4.5:1 | PASS |
| Desktop dark | --err / --hover-bg | #ff9eaa | #26383d | 6.25:1 | 4.5:1 | PASS |
| Desktop dark | --err / --selected-bg | #ff9eaa | #25413d | 5.64:1 | 4.5:1 | PASS |
| Desktop dark | --err / --accent-soft | #ff9eaa | #1d3734 | 6.51:1 | 4.5:1 | PASS |
| Desktop dark | --err / --code-bg | #ff9eaa | #111b1e | 8.94:1 | 4.5:1 | PASS |
| Desktop dark | --on-accent / --accent | #102a28 | #68d5c4 | 8.60:1 | 4.5:1 | PASS |
| Desktop dark | --on-accent / --accent-hover | #102a28 | #8ce3d5 | 10.16:1 | 4.5:1 | PASS |
| Desktop dark | --surface / --text-muted | #192428 | #b2c3c6 | 8.69:1 | 4.5:1 | PASS |
| Desktop dark | --surface / --err | #192428 | #ff9eaa | 8.10:1 | 4.5:1 | PASS |
| Desktop dark | --control-border / --bg | #70878b | #11181b | 4.72:1 | 3:1 | PASS |
| Desktop dark | --control-border / --bg-subtle | #70878b | #151e22 | 4.46:1 | 3:1 | PASS |
| Desktop dark | --control-border / --surface | #70878b | #192428 | 4.17:1 | 3:1 | PASS |
| Desktop dark | --control-border / --surface-alt | #70878b | #202e32 | 3.69:1 | 3:1 | PASS |
| Desktop dark | --focus-ring / --bg | #68d5c4 | #11181b | 10.17:1 | 3:1 | PASS |
| Desktop dark | --focus-ring / --bg-subtle | #68d5c4 | #151e22 | 9.59:1 | 3:1 | PASS |
| Desktop dark | --focus-ring / --surface | #68d5c4 | #192428 | 8.99:1 | 3:1 | PASS |
| Desktop dark | --focus-ring / --surface-alt | #68d5c4 | #202e32 | 7.94:1 | 3:1 | PASS |
| Desktop dark | --focus-ring / --hover-bg | #68d5c4 | #26383d | 6.94:1 | 3:1 | PASS |
| Desktop dark | --focus-ring / --selected-bg | #68d5c4 | #25413d | 6.26:1 | 3:1 | PASS |
| Desktop dark | --focus-ring / --accent-soft | #68d5c4 | #1d3734 | 7.22:1 | 3:1 | PASS |
| Desktop dark | --focus-ring / --code-bg | #68d5c4 | #111b1e | 9.92:1 | 3:1 | PASS |
| Desktop dark | --ok / --tint-ok su --surface | #7ad6ad | #213233 | 7.67:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint / --tint-ok su --surface | #a5b8bd | #213233 | 6.49:1 | 4.5:1 | PASS |
| Desktop dark | --warn / --tint-warn su --surface | #e8be6a | #2c322e | 7.51:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint / --tint-warn su --surface | #a5b8bd | #2c322e | 6.37:1 | 4.5:1 | PASS |
| Desktop dark | --err / --err 8% su --surface | #ff9eaa | #2b2e32 | 6.97:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint (opacity 1) / --surface | #a5b8bd | #192428 | 7.70:1 | 4.5:1 | PASS |
| Desktop dark | --text-faint (opacity 1) / --bg-subtle | #a5b8bd | #151e22 | 8.21:1 | 4.5:1 | PASS |
| Desktop dark | --text-muted (opacity 1) / --surface-alt | #b2c3c6 | #202e32 | 7.69:1 | 3:1 | PASS |
| Desktop dark | --accent (opacity 1) / --surface-alt | #68d5c4 | #202e32 | 7.94:1 | 3:1 | PASS |
| Mobile dark | --testo / --bg | #edf3f3 | #11181b | 15.99:1 | 4.5:1 | PASS |
| Mobile dark | --testo / --panel | #edf3f3 | #192226 | 14.42:1 | 4.5:1 | PASS |
| Mobile dark | --testo / --panel-hover | #edf3f3 | #223036 | 12.12:1 | 4.5:1 | PASS |
| Mobile dark | --testo / --utente | #edf3f3 | #244941 | 8.89:1 | 4.5:1 | PASS |
| Mobile dark | --testo-dim / --bg | #acbdbf | #11181b | 9.22:1 | 4.5:1 | PASS |
| Mobile dark | --testo-dim / --panel | #acbdbf | #192226 | 8.31:1 | 4.5:1 | PASS |
| Mobile dark | --testo-dim / --panel-hover | #acbdbf | #223036 | 6.99:1 | 4.5:1 | PASS |
| Mobile dark | --testo-dim / --utente | #acbdbf | #244941 | 5.13:1 | 4.5:1 | PASS |
| Mobile dark | --su-accento / --accento | #102a28 | #63d5c5 | 8.57:1 | 4.5:1 | PASS |
| Mobile dark | --su-accento / --accento-hover | #102a28 | #83e1d3 | 9.88:1 | 4.5:1 | PASS |
| Mobile dark | --su-accento / --accento-active | #102a28 | #50b9aa | 6.40:1 | 4.5:1 | PASS |
| Mobile dark | Normale | #3b181c | #e9a3a3 | 7.67:1 | 4.5:1 | PASS |
| Mobile dark | Premuto | #3b181c | #d68d8d | 6.04:1 | 4.5:1 | PASS |
| Mobile dark | --bordo-control / --bg | #70878b | #11181b | 4.72:1 | 3:1 | PASS |
| Mobile dark | --accento / --bg | #63d5c5 | #11181b | 10.13:1 | 3:1 | PASS |
| Mobile dark | --bordo-control / --panel | #70878b | #192226 | 4.26:1 | 3:1 | PASS |
| Mobile dark | --accento / --panel | #63d5c5 | #192226 | 9.13:1 | 3:1 | PASS |
| Mobile dark | --bordo-control / --panel-hover | #70878b | #223036 | 3.58:1 | 3:1 | PASS |
| Mobile dark | --accento / --panel-hover | #63d5c5 | #223036 | 7.68:1 | 3:1 | PASS |
| Mobile dark | --errore / --errore-bg | #ffb8ad | #39262a | 8.56:1 | 4.5:1 | PASS |
| Mobile dark | --testo-dim (opacity 0.85) / --bg | #95a4a6 | #11181b | 6.97:1 | 4.5:1 | PASS |
| Mobile dark | --errore (opacity 0.85) / --bg | #dba097 | #11181b | 8.12:1 | 4.5:1 | PASS |
| Mobile dark | --testo / .msg code su --panel | #edf3f3 | #2b3538 | 11.27:1 | 4.5:1 | PASS |
| Mobile dark | --testo / .msg code su --utente | #edf3f3 | #345750 | 7.13:1 | 4.5:1 | PASS |
| Mobile dark | --testo / .msg.pending | #edf3f3 | #1b2f2f | 12.57:1 | 4.5:1 | PASS |
| Mobile dark | --testo-dim / .msg.pending.risposta-data | #acbdbf | #162325 | 8.26:1 | 4.5:1 | PASS |
| Mobile dark | --ok / badge, opacità minima 0.88 | #73c9a5 | #1f3c34 | 6.10:1 | 4.5:1 | PASS |

</details>
