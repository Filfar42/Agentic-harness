"""Dialogo nativo di selezione cartella, aperto sulla macchina dell'utente.

Perche' lato server
-------------------
Il browser non puo', per ragioni di sicurezza, comunicare a una pagina web il
percorso di una cartella scelta dall'utente. L'harness pero' gira in locale:
il "server" e' la stessa macchina davanti a cui si sta seduti, quindi puo'
aprire il selettore del sistema operativo -- Esplora risorse su Windows, il
Finder su macOS -- che e' quello che l'utente gia' conosce e sa usare in
fretta.

Su Windows si usa direttamente ``IFileOpenDialog`` via COM: e' il dialogo
moderno di Esplora risorse, con barra dei percorsi, ricerca e preferiti. Il
``askdirectory`` di Tkinter aprirebbe invece il vecchio ``SHBrowseForFolder``,
un alberello spartano senza barra di ricerca. Tkinter resta come rete di
sicurezza dove COM non e' disponibile.

Nota: il dialogo si apre sulla macchina che ESEGUE il server. L'harness si
lega a 127.0.0.1, quindi le due coincidono sempre.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path


class DialogUnavailable(RuntimeError):
    """Nessun selettore grafico utilizzabile su questa macchina."""


# ---------------------------------------------------------------------------
# Windows: IFileOpenDialog (il vero dialogo di Esplora risorse)
# ---------------------------------------------------------------------------


def _pick_windows(initial: str | None) -> str | None:
    import ctypes
    from ctypes import POINTER, byref, c_void_p, c_wchar_p

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    ole32 = ctypes.OleDLL("ole32")
    shell32 = ctypes.OleDLL("shell32")

    def guid(text: str) -> GUID:
        out = GUID()
        ole32.CLSIDFromString(c_wchar_p(text), byref(out))
        return out

    CLSID_FileOpenDialog = guid("{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}")
    IID_IFileOpenDialog = guid("{D57C7288-D4AD-4768-BE02-9D969532D960}")
    IID_IShellItem = guid("{43826D1E-E718-42EE-BC55-A1E261C37BFE}")

    CLSCTX_INPROC_SERVER = 1
    FOS_PICKFOLDERS = 0x00000020
    FOS_FORCEFILESYSTEM = 0x00000040
    FOS_PATHMUSTEXIST = 0x00000800
    SIGDN_FILESYSPATH = 0x80058000
    HRESULT_CANCELLED = -2147023673  # 0x800704C7: l'utente ha chiuso il dialogo

    # Indici nella vtable. IUnknown occupa 0-2, IModalWindow::Show e' 3, poi
    # seguono i metodi di IFileDialog nell'ordine dichiarato nell'header.
    RELEASE, SHOW = 2, 3
    SET_OPTIONS, GET_OPTIONS, SET_FOLDER, SET_TITLE, GET_RESULT = 9, 10, 12, 17, 20
    ITEM_GET_DISPLAY_NAME = 5

    def call(interface: c_void_p, index: int, *argtypes_and_args):
        """Invoca il metodo ``index`` della vtable di ``interface``."""
        argtypes = [c_void_p] + [a[0] for a in argtypes_and_args]
        args = [a[1] for a in argtypes_and_args]
        vtable = ctypes.cast(interface, POINTER(POINTER(c_void_p))).contents
        proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, *argtypes)
        return proto(vtable[index])(interface, *args)

    ole32.CoInitialize(None)
    dialog = c_void_p()
    try:
        ole32.CoCreateInstance(
            byref(CLSID_FileOpenDialog), None, CLSCTX_INPROC_SERVER,
            byref(IID_IFileOpenDialog), byref(dialog),
        )

        options = ctypes.c_uint32()
        call(dialog, GET_OPTIONS, (POINTER(ctypes.c_uint32), byref(options)))
        call(dialog, SET_OPTIONS, (ctypes.c_uint32,
             options.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST))
        call(dialog, SET_TITLE, (c_wchar_p, "Scegli la cartella di lavoro"))

        if initial and Path(initial).is_dir():
            folder = c_void_p()
            try:
                shell32.SHCreateItemFromParsingName(
                    c_wchar_p(str(Path(initial).resolve())), None,
                    byref(IID_IShellItem), byref(folder),
                )
                call(dialog, SET_FOLDER, (c_void_p, folder))
            except OSError:
                pass  # cartella iniziale non impostabile: si apre dove capita
            finally:
                if folder:
                    call(folder, RELEASE)

        try:
            call(dialog, SHOW, (c_void_p, None))
        except OSError as exc:
            if getattr(exc, "winerror", None) == HRESULT_CANCELLED:
                return None
            raise

        item = c_void_p()
        call(dialog, GET_RESULT, (POINTER(c_void_p), byref(item)))
        try:
            path = c_wchar_p()
            call(item, ITEM_GET_DISPLAY_NAME,
                 (ctypes.c_uint32, SIGDN_FILESYSPATH), (POINTER(c_wchar_p), byref(path)))
            chosen = path.value
            ole32.CoTaskMemFree(path)
            return chosen
        finally:
            call(item, RELEASE)
    finally:
        if dialog:
            call(dialog, RELEASE)
        ole32.CoUninitialize()


# ---------------------------------------------------------------------------
# macOS e Linux
# ---------------------------------------------------------------------------


def _pick_macos(initial: str | None) -> str | None:
    start = f' default location POSIX file "{initial}"' if initial else ""
    script = f'POSIX path of (choose folder with prompt "Cartella di lavoro"{start})'
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None  # annullato
    return result.stdout.strip() or None


def _pick_linux(initial: str | None) -> str | None:
    if shutil.which("zenity"):
        command = ["zenity", "--file-selection", "--directory",
                   "--title=Cartella di lavoro"]
        if initial:
            command.append(f"--filename={initial}/")
    elif shutil.which("kdialog"):
        command = ["kdialog", "--getexistingdirectory", initial or "."]
    else:
        return _pick_tk(initial)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _pick_tk(initial: str | None) -> str | None:
    """Ripiego portabile. Su Windows apre il vecchio selettore ad albero."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise DialogUnavailable("Tkinter non disponibile.") from exc

    try:
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 - tipicamente: nessun display
        raise DialogUnavailable(f"Nessun ambiente grafico disponibile ({exc}).") from exc

    try:
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            initialdir=initial or str(Path.home()), title="Cartella di lavoro"
        )
        return chosen or None
    finally:
        root.destroy()


def pick_folder(initial: str | None = None) -> str | None:
    """Apre il selettore nativo. ``None`` se l'utente annulla.

    Solleva :class:`DialogUnavailable` se non c'e' modo di mostrare un
    dialogo (macchina senza ambiente grafico).
    """
    system = platform.system()
    try:
        if system == "Windows":
            try:
                return _pick_windows(initial)
            except Exception:  # noqa: BLE001 - COM capriccioso: si ripiega su Tk
                return _pick_tk(initial)
        if system == "Darwin":
            return _pick_macos(initial)
        return _pick_linux(initial)
    except DialogUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DialogUnavailable(f"{type(exc).__name__}: {exc}") from exc
