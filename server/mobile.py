"""Seconda interfaccia dell'harness, pensata per il telefono.

Non e' un secondo cervello: e' un **ponte**. Tutte le chiamate ``/api/*``
attraversano questo processo e finiscono sul server principale
(``python run.py``, di default su ``127.0.0.1:8123``), che resta l'unico a
leggere impostazioni, eseguire turni e scrivere le sessioni. E' questa scelta
che tiene le due interfacce sincronizzate per costruzione: un messaggio
spedito dal telefono appare sul desktop al prossimo refresh, e viceversa,
perche' lo stato ce n'e' uno solo -- quello del processo principale.

Lo streaming SSE viene girato pezzo per pezzo senza buffering, quindi i
passi di pensiero si vedono dal telefono mentre il modello li produce.

Avvio: ``python run_mobile.py`` (ascolta su 0.0.0.0:8200, visibile in LAN).
L'indirizzo del server principale si cambia con ``--upstream`` o con la
variabile d'ambiente ``HARNESS_UPSTREAM``.

## Perche' c'e' un token

``0.0.0.0`` vuol dire "chiunque sia sulla rete". Dietro questo ponte c'e' un
agente che esegue comandi sul computer dell'utente: senza una chiave, il
telefono del vicino di casa -- o qualunque cosa parli sul Wi-Fi
dell'aeroporto -- avrebbe la stessa autorita' del padrone di casa. Il token
si genera all'avvio, viaggia una volta sola nell'URL (``?k=...``) e poi vive
in un cookie: un gesto solo, la prima volta che si apre il link.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

WEB_MOBILE_DIR = Path(__file__).resolve().parent.parent / "web_mobile"

DEFAULT_UPSTREAM = "http://127.0.0.1:8123"

# Intestazioni che non hanno senso ritrasmettere: parlano della singola
# connessione, non della risposta. ``host`` in particolare va tolto, senno'
# il client a monte finirebbe nell'intestazione verso il server principale.
_HOP_BY_HOP = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "upgrade",
}


def upstream_base() -> str:
    return os.environ.get("HARNESS_UPSTREAM", DEFAULT_UPSTREAM).rstrip("/")


# Un solo client per il processo: connection pooling verso l'upstream e,
# soprattutto, timeout di lettura infinito. Su un turno agentico fra un tool
# e l'altro possono passare minuti di silenzio sullo stream: il default di
# httpx (5 secondi) taglierebbe il filo a meta' ragionamento.
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=f"{upstream_base()}/",
            timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=None),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


TOKEN_ENV = "HARNESS_MOBILE_TOKEN"
COOKIE = "harness_mobile"
# Trenta giorni: il telefono si ricollega da solo per un mese, poi il link va
# riaperto. Piu' corto sarebbe una seccatura quotidiana, piu' lungo un cookie
# che nessuno ricorda di avere.
COOKIE_MAX_AGE = 30 * 24 * 3600


def token() -> str:
    """La chiave d'accesso di questo ponte.

    Si legge dall'ambiente **alla chiamata**, non all'import: ``run.py`` la
    imposta prima di sollevare il thread, e i test la sostituiscono con una
    loro. Se non c'e', se ne genera una e la si stampa: mai nessun accesso
    senza chiave, neppure quando qualcuno lancia ``uvicorn server.mobile:app``
    a mano.
    """
    esistente = os.environ.get(TOKEN_ENV)
    if esistente:
        return esistente
    nuovo = secrets.token_urlsafe(9)
    os.environ[TOKEN_ENV] = nuovo
    print(f"  Chiave dell'interfaccia mobile: {nuovo}  (aggiungi ?k={nuovo} all'indirizzo)")
    return nuovo


@asynccontextmanager
async def lifespan(_app: FastAPI):
    token()  # generata e stampata all'avvio, non alla prima richiesta
    yield
    await close_client()


app = FastAPI(title="Harness Mobile", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def guardia_del_token(request: Request, call_next):
    """Nessuna risposta senza chiave, nemmeno un file statico.

    Tre modi per portarla, in ordine di comodita': la query ``?k=`` (il link
    che si apre la prima volta), il cookie che quella query lascia, e
    l'intestazione ``X-Harness-Token`` per chi chiama da uno script.
    """
    atteso = token()
    dalla_query = request.query_params.get("k") or ""
    presentate = (
        dalla_query,
        request.headers.get("x-harness-token") or "",
        request.cookies.get(COOKIE) or "",
    )
    # Basta che UNA sia giusta, non la prima in ordine: un cookie valido non
    # deve perdere contro un'intestazione vecchia rimasta appesa a un client.
    # compare_digest e non ``==``: il confronto a tempo costante e' gratis.
    if not any(secrets.compare_digest(c, atteso) for c in presentate):
        return JSONResponse(
            {
                "detail": (
                    "Chiave mancante o sbagliata. Apri l'indirizzo completo "
                    "stampato dal terminale, quello che finisce con ?k=..."
                )
            },
            status_code=401,
        )
    risposta = await call_next(request)
    if dalla_query:
        # Arrivata dalla query: la si deposita, cosi' le richieste della
        # pagina (fetch, EventSource, immagini) non devono portarsela dietro.
        risposta.set_cookie(
            COOKIE, atteso, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax"
        )
    return risposta


# ---------------------------------------------------------------------------
# Stato del ponte
# ---------------------------------------------------------------------------


@app.get("/api/mobile/health")
async def health() -> JSONResponse:
    """Il telefono puo' distinguere 'rete giu'' da 'server principale spento'."""
    try:
        response = await get_client().get("api/sessions")
        reachable = response.status_code < 500
        detail: Any = None
    except httpx.HTTPError as exc:
        reachable = False
        detail = f"{type(exc).__name__}: {exc}"
    return JSONResponse(
        {
            "ok": reachable,
            "upstream": upstream_base(),
            "detail": detail,
        }
    )


# ---------------------------------------------------------------------------
# Il ponte: tutto il resto sotto /api passa attraverso
# ---------------------------------------------------------------------------


def _filtered_headers(headers: Any) -> dict[str, str]:
    return {
        k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP
    }


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_api(path: str, request: Request) -> StreamingResponse:
    body = await request.body()
    client = get_client()
    # La chiave del ponte non deve proseguire verso il server principale: li'
    # non significa niente e finirebbe nei log di un altro processo.
    parametri = {k: v for k, v in request.query_params.items() if k != "k"}
    try:
        # Il pattern cattura il percorso DOPO "/api": va rimesso il prefisso,
        # senno' "/api/sessions" finirebbe su "/sessions" upstream (404).
        upstream_response = await client.send(
            client.build_request(
                request.method,
                f"api/{path}",
                params=parametri,
                content=body if body else None,
                headers=_filtered_headers(request.headers),
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        # Errore chiaro e in italiano: sul telefono deve capirsi al volo se
        # il problema e' la rete o il server principale non avviato.
        raise HTTPException(
            502,
            f"Server principale non raggiungibile su {upstream_base()} "
            f"({type(exc).__name__}). Avvialo con 'python run.py'.",
        ) from exc

    async def flow() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream_response.aiter_bytes():
                yield chunk
        finally:
            await upstream_response.aclose()

    # Niente media_type esplicito: il Content-Type dell'upstream (compreso
    # text/event-stream) viaggia gia' nelle intestazioni filtrate.
    return StreamingResponse(
        flow(),
        status_code=upstream_response.status_code,
        headers=_filtered_headers(upstream_response.headers),
    )


# ---------------------------------------------------------------------------
# La pagina
# ---------------------------------------------------------------------------

app.mount(
    "/static",
    StaticFiles(directory=str(WEB_MOBILE_DIR)),
    name="static-mobile",
)


def _asset_version() -> str:
    """Impronta di app.js e style.css: cambia il file, cambia l'URL.

    Senza questo il browser del telefono continua a eseguire il vecchio
    JavaScript per ore (la freschezza euristica e' aggressiva su LAN) e un
    fix all'interfaccia "non si vede". Con ``?v=`` ogni modifica invalida
    da sola la cache.
    """
    imprint = hashlib.sha256()
    for nome in ("app.js", "style.css"):
        try:
            imprint.update((WEB_MOBILE_DIR / nome).read_bytes())
        except OSError:
            imprint.update(nome.encode())
    return imprint.hexdigest()[:10]


@app.get("/")
def index() -> Response:
    """La pagina, con gli asset referenziati alla loro versione esatta."""
    html = (WEB_MOBILE_DIR / "index.html").read_text(encoding="utf-8")
    versione = _asset_version()
    html = html.replace("/static/app.js", f"/static/app.js?v={versione}")
    html = html.replace("/static/style.css", f"/static/style.css?v={versione}")
    return Response(
        html,
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )
