"""HTTP trust boundaries for the local application and its mobile bridge.

Browser origin checks are enforced before application routes: CORS alone
does not prevent cross-origin requests from executing side effects. Remote
clients of the desktop API require an explicitly configured shared secret.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import secrets
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Receive, Scope, Send, Message

API_TOKEN_ENV = "HARNESS_API_TOKEN"
API_TOKEN_HEADER = "x-harness-api-token"
MAX_REQUEST_BYTES = 32 * 1024 * 1024


def same_origin(origin: str, scheme: str, host: str) -> bool:
    """Compare complete HTTP origins, rejecting opaque and malformed ones."""
    try:
        left, right = urlsplit(origin), urlsplit(f"{scheme}://{host}")
        if left.scheme not in {"http", "https"} or left.username or left.password:
            return False
        return (
            left.scheme, left.hostname, left.port or (443 if left.scheme == "https" else 80)
        ) == (
            right.scheme, right.hostname, right.port or (443 if right.scheme == "https" else 80)
        ) and left.path in {"", "/"} and not left.query and not left.fragment
    except ValueError:
        return False


def browser_request_allowed(scope: Scope, headers: Headers) -> bool:
    """Reject foreign browser origins, including same-site preview ports."""
    origin = headers.get("origin")
    if origin and not same_origin(origin, scope.get("scheme", "http"), headers.get("host", "")):
        return False
    return headers.get("sec-fetch-site", "") not in {"cross-site", "same-site"}


class LocalAccessGuard:
    """Keep desktop HTTP local by default; guard DNS rebinding and CSRF.

The peer comes from the ASGI transport, never a forwarded HTTP header.
Non-IP peers exist only in in-process ASGI transports (for example TestClient).
Deployment with a reverse proxy must preserve this boundary at that proxy.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        configured = os.environ.get(API_TOKEN_ENV, "")
        supplied = headers.get(API_TOKEN_HEADER, "")
        authenticated = bool(configured) and secrets.compare_digest(
            configured.encode(), supplied.encode()
        )
        peer = (scope.get("client") or ("", 0))[0]
        try:
            peer_ip = ipaddress.ip_address(peer)
        except ValueError:
            peer_ip = None
        forbidden = not browser_request_allowed(scope, headers)
        if peer_ip is not None and not authenticated:
            forbidden |= not peer_ip.is_loopback
            try:
                hostname = urlsplit("http://" + headers.get("host", "")).hostname or ""
                local_host = hostname.lower() == "localhost"
                if not local_host:
                    local_host = ipaddress.ip_address(hostname).is_loopback
                forbidden |= not local_host
            except ValueError:
                forbidden = True
        if forbidden:
            await JSONResponse({"detail": "Origine o client non autorizzato."}, 403)(scope, receive, send)
            return
        try:
            oversized = int(headers.get("content-length", "0")) > MAX_REQUEST_BYTES
        except ValueError:
            await JSONResponse({"detail": "Content-Length non valido."}, 400)(scope, receive, send)
            return
        if oversized:
            await JSONResponse({"detail": "Richiesta troppo grande (massimo 32 MiB)."}, 413)(scope, receive, send)
            return

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers.setdefault("X-Content-Type-Options", "nosniff")
                response_headers.setdefault("Referrer-Policy", "no-referrer")
                response_headers.setdefault("X-Frame-Options", "SAMEORIGIN")
            await send(message)

        received = 0
        body_complete = False

        async def bounded_receive() -> Message:
            nonlocal received, body_complete
            if body_complete:
                return await receive()
            try:
                message = await asyncio.wait_for(receive(), timeout=30.0)
            except TimeoutError as exc:
                raise HTTPException(408, "Timeout durante la lettura della richiesta.") from exc
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_REQUEST_BYTES:
                    raise HTTPException(413, "Richiesta troppo grande (massimo 32 MiB).")
                body_complete = not message.get("more_body", False)
            return message

        await self.app(scope, bounded_receive, secured_send)
