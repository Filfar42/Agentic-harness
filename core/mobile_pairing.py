"""Un solo browser associato al desktop. I codici QR vivono solo in memoria."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

PAIRING_TTL = 300
DEVICE_TTL = 365 * 24 * 3600


class PairingError(ValueError):
    pass


class MobilePairing:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.pending: tuple[str, float] | None = None

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Formato non valido")
            if data and (
                not isinstance(data.get("hash"), str)
                or len(data["hash"]) != 64
                or any(c not in "0123456789abcdef" for c in data["hash"])
                or not isinstance(data.get("name"), str)
                or any(not isinstance(data.get(k), (int, float))
                       or not math.isfinite(data[k]) for k in ("paired_at", "expires_at"))
            ):
                raise ValueError("Associazione incompleta")
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # Un archivio illeggibile non deve liberare il posto per un altro telefono.
            raise PairingError("Associazione non leggibile. Revocala dal desktop e riprova.") from exc

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".mobile-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _device(self) -> dict:
        data = self._read()
        return data if data.get("expires_at", 0) > time.time() else {}

    def status(self) -> dict:
        with self.lock:
            device = self._device()
            return {
                "device": {k: device[k] for k in ("name", "paired_at", "expires_at")}
                if device else None,
                "pending_expires_at": self.pending[1]
                if self.pending and self.pending[1] > time.time() else None,
            }

    def create(self) -> tuple[str, float]:
        with self.lock:
            if self._device():
                raise PairingError("Revoca il telefono associato prima di associarne un altro.")
            code = secrets.token_urlsafe(32)
            self.pending = (hashlib.sha256(code.encode()).hexdigest(), time.time() + PAIRING_TTL)
            return code, self.pending[1]

    def claim(self, code: str, user_agent: str) -> str:
        with self.lock:
            if (self._device() or not self.pending or self.pending[1] <= time.time()
                    or not secrets.compare_digest(self.pending[0], hashlib.sha256(code.encode()).hexdigest())):
                raise PairingError("QR scaduto o già utilizzato. Generane uno nuovo dal desktop.")
            credential = secrets.token_urlsafe(32)
            ua = user_agent.lower()
            name = "iPhone" if "iphone" in ua else "iPad" if "ipad" in ua else "Android" if "android" in ua else "Browser mobile"
            self._write({"hash": hashlib.sha256(credential.encode()).hexdigest(), "name": name,
                         "paired_at": time.time(), "expires_at": time.time() + DEVICE_TTL})
            self.pending = None
            return credential

    def authorized(self, credential: str) -> bool:
        with self.lock:
            device = self._device()
            return bool(credential and device and secrets.compare_digest(
                device.get("hash", ""), hashlib.sha256(credential.encode()).hexdigest()))

    def revoke(self) -> None:
        with self.lock:
            self._write({})
            self.pending = None
