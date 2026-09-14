"""Serve the real UI with explicitly simulated content for visual review.

No agent, command, model request, real session or user preference is touched.
Run python scripts/visual_preview.py, then open http://127.0.0.1:8137.
The UI sources are the production sources; only API responses are fixtures.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.config import DEFAULTS  # noqa: E402 -- support direct execution from scripts/

app = FastAPI(title="Harness visual preview — simulated data")
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="desktop")
app.mount("/mobile-static", StaticFiles(directory=ROOT / "web_mobile"), name="mobile")
app.mount("/brand", StaticFiles(directory=ROOT / "docs" / "brand"), name="brand")
SETTINGS = {
    **DEFAULTS,
    "workspace_dir": "D:/Workspace/Atlas",
    "model_name": "qwen3:32b",
    "theme_mode": "light",
    "docker_autostart": False,
    "system_prompt": "Visual preview",
}
SESSIONS = [
    {
        "id": "design",
        "title": "Raffinamento del design system",
        "updated_at": "2026-09-06T16:20:00",
        "n_messages": 8,
        "running": False,
    },
    {
        "id": "review",
        "title": "Revisione del flusso di autenticazione",
        "updated_at": "2026-09-06T15:10:00",
        "n_messages": 12,
    },
    {
        "id": "docs",
        "title": "Documentazione delle API",
        "updated_at": "2026-09-05T11:30:00",
        "n_messages": 6,
    },
]
PLAN = [
    {
        "id": 1,
        "text": "Verificare palette e contrasti",
        "status": "done",
        "note": "Coppie colore misurate",
    },
    {
        "id": 2,
        "text": "Rifinire gli stati interattivi",
        "status": "done",
        "note": "Focus e feedback uniformi",
    },
    {
        "id": 3,
        "text": "Controllare light e dark mode",
        "status": "todo",
        "note": "Revisione finale",
    },
]
MESSAGES = [
    {
        "role": "user",
        "content": "Rifinisci il design system mantenendo il layout. Controlla i contrasti e gli stati dei componenti.",
    },
    {
        "role": "assistant",
        "content": "Ho verificato i componenti principali e definito una palette coerente. Mantengo la struttura attuale e concentro le modifiche su **leggibilità, contrasto e feedback**.",
        "tool_calls": [
            {
                "id": "read1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"filepath":"web/style.css"}'},
            }
        ],
    },
    {
        "role": "tool",
        "name": "read_file",
        "tool_call_id": "read1",
        "args": {"filepath": "web/style.css"},
        "content": json.dumps(
            {
                "filepath": "web/style.css",
                "content": ":root {\n  --accent: #0b7169;\n  --text: #1c2b30;\n}",
            }
        ),
        "duration_s": 0.08,
        "ok": True,
    },
    {
        "role": "assistant",
        "content": "La revisione è pronta per il controllo visivo.\n\n- Gerarchie più nitide, anche nei testi secondari.\n- Focus da tastiera sempre riconoscibile.\n- Transizioni brevi e movimento ridotto rispettato.\n\n```css\n--motion-fast: 160ms;\n--motion-ui: 200ms;\n--ease-out: cubic-bezier(.2, .75, .25, 1);\n```\n\nIl comportamento dell'agente e la disposizione dei pannelli restano quelli esistenti.",
    },
]


def payload(session_id: str = "design") -> dict:
    """Return a fully local, deterministic visual fixture."""
    messages = [] if session_id == "new" else MESSAGES
    return {
        "session_id": session_id,
        "messages": messages,
        "messages_offset": 0,
        "messages_total": len(messages),
        "running": False,
        "pending": None,
        "plan": PLAN if messages else [],
        "notes": [],
        "preview": None,
        "workspace_dir": SETTINGS["workspace_dir"],
        "model": SETTINGS["model_name"],
        "session_workspace": SETTINGS["workspace_dir"],
        "stats": {
            "session_id": session_id,
            "context_used": 6240,
            "context_window": 32768,
            "attachments": [],
            "touched_files": ["web/style.css", "web/index.html"] if messages else [],
            "sessions": SESSIONS,
        },
    }


@app.get("/")
def desktop(baseline: bool = False) -> HTMLResponse:
    """Optional original CSS permits a geometry comparison against the same fixture."""
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    if baseline:
        markup = markup.replace("/static/style.css", "/brand/qa/before-desktop.css")
    for asset in ("app.js", "style.css", "companion.js", "companion.css"):
        version = (ROOT / "web" / asset).stat().st_mtime_ns
        markup = markup.replace(f'/static/{asset}"', f'/static/{asset}?v={version}"')
    return HTMLResponse(markup, headers={"Cache-Control": "no-store"})


@app.get("/mobile")
def mobile() -> HTMLResponse:
    markup = (ROOT / "web_mobile" / "index.html").read_text(encoding="utf-8")
    markup = markup.replace('"/static/', '"/mobile-static/')
    for asset in ("app.js", "style.css"):
        version = (ROOT / "web_mobile" / asset).stat().st_mtime_ns
        markup = markup.replace(f'/mobile-static/{asset}"', f'/mobile-static/{asset}?v={version}"')
    return HTMLResponse(markup, headers={"Cache-Control": "no-store"})


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def keepalive():
        while True:
            yield ": visual preview\n\n"
            await asyncio.sleep(12)

    return StreamingResponse(keepalive(), media_type="text/event-stream")


@app.api_route("/api/{path:path}", methods=["GET", "POST", "DELETE"])
async def fixture(path: str, request: Request) -> dict:
    backend = {
        "name": "Ollama",
        "online": True,
        "models": ["qwen3:32b"],
        "model_name": "qwen3:32b",
        "url": "http://127.0.0.1:11434",
        "detail": "Anteprima con dati simulati",
    }
    if path == "bootstrap":
        return {
            "app": {"name": "Local Agent Harness", "version": "2.36.1"},
            "settings": SETTINGS,
            "session": payload(),
            "memories": [],
            "backend": backend,
        }
    if path in {"backend", "models", "ping"}:
        return backend
    if path == "sessions":
        return payload("new") if request.method == "POST" else {"sessions": SESSIONS}
    if path.startswith("sessions/"):
        return payload(path.split("/")[1])
    if path == "sandbox":
        return {
            "mode": "docker",
            "available": True,
            "running": True,
            "detail": "Sandbox simulata per anteprima visiva",
            "workdir": "/workspace",
            "project_image": "harness-workspace:latest",
            "network": False,
        }
    if path == "settings":
        body = await request.json()
        SETTINGS.update(body.get("values", {}))
        return {"settings": SETTINGS, "ok": True}
    if path == "vaults":
        return {"vaults": []}
    if path == "memories":
        return {"memories": []}
    if path == "chat":
        return {"ok": False, "detail": "Anteprima visiva: invio disabilitato."}
    return {"ok": True, "checks": [], "ready": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8137, log_level="warning")
