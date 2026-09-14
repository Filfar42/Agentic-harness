"""Build a complete source archive and a reproducible file-level audit index.

Run from this checkout after tests. The baseline is read-only; output stays
inside the workspace. Third-party dependencies, caches and runtime data are
excluded explicitly. Zip integrity and entry hashes are verified after write.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import platform
import sys
import zipfile
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE_DIRS = ("core", "server", "web", "web_mobile", "tests", "skills", "scripts")
TOP_FILES = ("run.py", "run_mobile.py", "README.md", "requirements.txt", "pyproject.toml",
             "uv.lock", "Dockerfile.sandbox")
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".venv"}


def digest(path: Path) -> str:
    """SHA-256 of the exact bytes delivered."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources() -> list[Path]:
    """Inventory application-owned sources, tests, scripts and static assets."""
    paths = [ROOT / name for name in TOP_FILES if (ROOT / name).is_file()]
    for directory in SOURCE_DIRS:
        paths.extend(path for path in (ROOT / directory).rglob("*")
                     if path.is_file() and not EXCLUDE_DIRS.intersection(path.parts)
                     and path.suffix not in {".pyc", ".pyo"})
    return sorted(paths, key=lambda path: path.relative_to(ROOT).as_posix())


def scope(path: Path, changed: bool) -> str:
    """Describe evidence coverage without claiming manual review of every line."""
    relative = path.relative_to(ROOT)
    first = relative.parts[0]
    if path.suffix in {".png", ".ico", ".jpg", ".woff2"}:
        return "Asset statico: inventario e integrità; non analisi pixel o decodificatore."
    if first == "tests":
        return "Suite raccolta ed eseguita; skip specifici in VALIDAZIONE.md."
    if first in {"core", "server"}:
        return ("Confini modificati e review del diff; compilazione AST, Ruff e regressioni."
                if changed else "Controllo strutturale/operazioni sensibili, AST, Ruff e suite correlata; conservato.")
    if first in {"web", "web_mobile"}:
        return "Revisione streaming/rendering/confine API e test UI esistenti; nessun benchmark browser completo."
    if first == "scripts":
        return "Script di consegna: eseguito, archivio verificato con CRC e hash."
    return "Configurazione/documentazione: inventario, lettura e verifica coerenza con runtime."


def main() -> None:
    """Write reports, patch, source manifest, and a verified ZIP."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=Path("D:/Astra-audit-original-20260905"))
    args = parser.parse_args()
    baseline = args.baseline.resolve(strict=True)
    output = ROOT / "docs" / "audit"
    output.mkdir(parents=True, exist_ok=True)
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    paths = sources()
    records = []
    patch = []
    rows = []
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        old = baseline / relative
        sha = digest(path)
        old_sha = digest(old) if old.is_file() else None
        changed = old_sha != sha
        state = "nuovo" if old_sha is None else "modificato" if changed else "invariato"
        records.append({"path": relative, "bytes": path.stat().st_size,
                        "sha256": sha, "baseline_sha256": old_sha, "state": state})
        rows.append(f"| `{relative}` | {state} | {scope(path, changed)} |")
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)
        if changed:
            try:
                current = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
                previous = old.read_text(encoding="utf-8-sig").splitlines(keepends=True) if old.is_file() else []
            except UnicodeError:
                continue
            patch.extend(difflib.unified_diff(previous, current,
                         fromfile=f"a/{relative}" if old.is_file() else "/dev/null",
                         tofile=f"b/{relative}"))
    (output / "MANIFEST.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    (output / "changes.patch").write_text("".join(patch), encoding="utf-8")
    counts = {state: sum(row["state"] == state for row in records)
              for state in ("nuovo", "modificato", "invariato")}
    inventory = ("# Inventario della consegna\n\n"
                 f"{len(records)} file di progetto: {counts['modificato']} modificati, "
                 f"{counts['nuovo']} nuovi, {counts['invariato']} invariati. "
                 "Il manifest contiene dimensioni e SHA-256 prima/dopo.\n\n"
                 "I livelli di esame sono dichiarati per file. Inventariare o eseguire una suite "
                 "non equivale a dimostrare assenza di vulnerabilità. Le note di audit sono "
                 "aggiunte all'archivio oltre a questi file; cache, ambiente virtuale e dati "
                 "utente sono esclusi. Nessun file sorgente applicativo è sostituito da placeholder.\n\n"
                 "| File | Stato | Esame e verifiche |\n|---|---|---|\n" + "\n".join(rows) + "\n")
    (output / "INVENTARIO.md").write_text(inventory, encoding="utf-8")

    from core.system_prompt import SYSTEM_PROMPT, SYSTEM_PROMPT_LEAN
    from core.textutils import estimate_tokens

    (output / "SYSTEM_PROMPT_REVISED.md").write_text(
        "# System prompt effettivi\n\nEsportati da core/system_prompt.py, senza riassunti o omissioni. "
        "I vincoli operativi sono imposti anche dal runtime.\n\n"
        f"Stima interna: lean {estimate_tokens(SYSTEM_PROMPT_LEAN)} token; "
        f"esteso {estimate_tokens(SYSTEM_PROMPT)} token. Non è il tokenizer del modello.\n\n"
        "## Lean\n\n```text\n" + SYSTEM_PROMPT_LEAN + "```\n\n"
        "## Esteso\n\n```text\n" + SYSTEM_PROMPT + "```\n", encoding="utf-8")

    test_log = (ROOT / ".audit-final-tests.log").read_text(encoding="utf-8-sig")
    if "1195 passed, 7 skipped" not in test_log or "FAILED " in test_log:
        raise RuntimeError("Final test log does not match the reviewed successful run")
    lint_log = (ROOT / ".audit-lint.txt").read_text(encoding="utf-8-sig")
    if "All checks passed!" not in lint_log:
        raise RuntimeError("Lint verification is missing or unsuccessful")
    (output / "pytest-final.txt").write_text(test_log, encoding="utf-8")
    (output / "ruff-final.txt").write_text(lint_log, encoding="utf-8")
    dependency_lines = "\n".join(f"- {name}: {version(name)}" for name in (
        "fastapi", "starlette", "httpx", "uvicorn", "pydantic", "pytest", "ruff", "quickjs"))
    validation = f"""# Validazione effettivamente eseguita

Suite completa sul codice applicativo finale: **1195 passed, 7 skipped in 273.20s**.
Ruff: **All checks passed**. Tutti i sorgenti Python consegnati sono inoltre parsati con AST.
Log integrali: `pytest-final.txt` e `ruff-final.txt`.

Comandi usati nella virtualenv del workspace:

```powershell
.venv\\Scripts\\python.exe -m pytest -p no:cacheprovider -q -ra --tb=short
.venv\\Scripts\\python.exe -m ruff check core server tests run.py run_mobile.py --no-cache
```

La cache pytest è disabilitata perché la directory preesistente aveva permessi incompatibili;
questo non salta test. I test usano directory temporanee e fixture, senza leggere/scrivere
preferenze e conversazioni reali dell'utente.

## Ambiente

- Python: {platform.python_version()}
- Piattaforma: {platform.platform()}
{dependency_lines}

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
"""
    (output / "VALIDAZIONE.md").write_text(validation, encoding="utf-8")

    archive = artifacts / "astra-refactored-20260906.zip"
    included = [*paths, *sorted(path for path in output.rglob("*") if path.is_file())]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in included:
            bundle.write(path, path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(archive) as bundle:
        if bundle.testzip() is not None or len(bundle.namelist()) != len(included):
            raise RuntimeError("ZIP integrity or membership check failed")
        for path in included:
            stored = bundle.read(path.relative_to(ROOT).as_posix())
            if hashlib.sha256(stored).hexdigest() != digest(path):
                raise RuntimeError(f"Archive content mismatch: {path}")
    checksum = digest(archive)
    (archive.with_suffix(".zip.sha256")).write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size,
                      "sha256": checksum, "entries": len(included), "source_files": len(paths),
                      "source_counts": counts}, indent=2))


if __name__ == "__main__":
    main()
