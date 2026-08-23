---
nome: rilascio
descrizione: come si taglia una versione di questo progetto
termini: rilascio, release, versione, tag, changelog
---
Prima di dichiarare una versione pronta:

1. `python -m pytest -q` deve essere verde, senza eccezioni.
2. `ruff check .` deve passare.
3. `APP_VERSION` in `core/config.py` va alzata: terzo numero per una
   correzione, secondo per una funzione nuova.
4. Le modifiche all'interfaccia vanno provate in un browser vero prima di
   dichiararle fatte: uno screenshot non basta, serve un clic.

Non toccare `pyproject.toml`: la versione lì è ferma di proposito.
