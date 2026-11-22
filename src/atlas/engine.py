"""A language model that runs inside the app, on this computer.

The desktop app ships llama.cpp's ``llama-server`` (https://github.com/ggml-org/llama.cpp),
a program that runs a model from one file and answers the same HTTP API Atlas
already speaks to a farm. Nothing else comes with the app: the model files are
large and are downloaded once, on request, into the settings folder.

Three sizes of model for writing, and one for text vectors:

- **light** runs on almost anything and answers fast, but a model this small
  often fails to stay with the sources;
- **medium** is the one to start with on a recent laptop;
- **advanced** wants 16 GB of memory and patience without a graphics card;
- **vectors** is EmbeddingGemma 2, the very file a LlmOnLan farm serves, so
  vectors computed here stay valid when a farm takes over, and the other way.

Two servers at most run, each on a port of this computer only, behind a key
made up at start: one for writing, one for vectors. They stop with the app.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import settings

VECTOR_CONTRACT = "embeddinggemma-2/768/v1"
CATALOGUE = {
    "light": {
        "label": "Light",
        "name": "Gemma 3 1B",
        "file": "gemma-3-1b-it-Q4_K_M.gguf",
        "url": "https://huggingface.co/ggml-org/gemma-3-1b-it-GGUF/resolve/main/gemma-3-1b-it-Q4_K_M.gguf",
        "bytes": 806_000_000,
        "memory": 4,
        "for": "Any computer. Quick, but often loses the thread of the sources: expect statements "
        "set aside and thin answers.",
    },
    "medium": {
        "label": "Medium",
        "name": "Gemma 4 E2B",
        "file": "gemma-4-E2B-it-Q4_K_M.gguf",
        "url": "https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/resolve/main/gemma-4-E2B-it-Q4_K_M.gguf",
        "bytes": 3_110_000_000,
        "memory": 8,
        "for": "Most computers with 8 GB of memory or more. The one to start with.",
    },
    "advanced": {
        "label": "Advanced",
        "name": "Gemma 4 E4B",
        "file": "gemma-4-E4B-it-Q4_K_M.gguf",
        "url": "https://huggingface.co/ggml-org/gemma-4-E4B-it-GGUF/resolve/main/gemma-4-E4B-it-Q4_K_M.gguf",
        "bytes": 5_340_000_000,
        "memory": 16,
        "for": "16 GB of memory or more. Better answers, slow without a graphics card.",
    },
    "vectors": {
        "label": "Vectors",
        "name": "EmbeddingGemma 2",
        "file": "embeddinggemma-2-BF16.gguf",
        # The same pinned file a LlmOnLan farm serves: same text, same vector.
        "url": "https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/resolve/"
        "bfcd298762cc34d0357ece5ebdd31791a3a374d8/embeddinggemma-2-BF16.gguf",
        "bytes": 557_950_176,
        "sha256": "68bae29d62fb8c7d23e98d21fd4662753ddd636e6b62b8a70dfe059a9844f216",
        "memory": 2,
        "for": "Places bookmarks and finds passages by meaning, in any language.",
    },
}
WRITERS = ("light", "medium", "advanced")
START_WAIT = 180  # seconds a model may take to load


class EngineError(RuntimeError):
    """Something the person can act on."""


# ---- where things are ---------------------------------------------------------


def engine_dir() -> Path:
    return Path(os.environ.get("ATLAS_ENGINE_DIR") or settings.config_dir() / "engine")


def binary() -> Path | None:
    """llama-server: the one shipped with the app, else one found on this computer."""
    name = "llama-server.exe" if sys.platform.startswith("win") else "llama-server"
    shipped = engine_dir() / name
    if shipped.is_file():
        return shipped
    found = shutil.which("llama-server")
    return Path(found) if found else None


def models_dir() -> Path:
    return Path(os.environ.get("ATLAS_MODELS_DIR") or settings.config_dir() / "models")


def model_path(key: str) -> Path:
    return models_dir() / CATALOGUE[key]["file"]


def memory_gb() -> float | None:
    """The memory of this computer, to advise on a size. None when it cannot be told."""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong)
                    for name in ("total", "free", "tp", "fp", "tv", "fv", "fev")
                ]

            status = Status()
            status.length = ctypes.sizeof(Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return round(status.total / 2**30, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (OSError, ValueError, AttributeError):
        return None


def advised() -> str:
    memory = memory_gb()
    if memory is None or memory >= 15:
        return "medium" if memory is None or memory < 23 else "advanced"
    return "medium" if memory >= 7 else "light"


# ---- downloads ----------------------------------------------------------------

_downloads: dict[str, dict] = {}
_lock = threading.Lock()


def _fetch(key: str, opener=None) -> None:
    """Download one model file, resuming a part already there, then check it."""
    entry = CATALOGUE[key]
    state = _downloads[key]
    target = model_path(key)
    part = target.with_name(target.name + ".part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent": "BookmarkAtlas"}
        if have:
            headers["Range"] = f"bytes={have}-"
        request = urllib.request.Request(entry["url"], headers=headers)  # noqa: S310
        with (opener or urllib.request.urlopen)(request, timeout=60) as response:  # noqa: S310
            if have and response.status != 206:
                have = 0  # the server sent the whole file again
            length = response.headers.get("Content-Length")
            state["total"] = have + int(length) if length else entry["bytes"]
            with part.open("ab" if have else "wb") as out:
                state["bytes"] = have
                while not state["cancel"]:
                    block = response.read(1 << 20)
                    if not block:
                        break
                    out.write(block)
                    state["bytes"] += len(block)
        if state["cancel"]:
            state.update(status="cancelled")
            return
        if entry.get("sha256"):
            state["status"] = "checking"
            digest = hashlib.sha256()
            with part.open("rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
            if digest.hexdigest() != entry["sha256"]:
                part.unlink(missing_ok=True)
                raise EngineError("The file that arrived is not the expected one. Try again.")
        elif part.stat().st_size < entry["bytes"] * 0.5:
            raise EngineError("The download stopped early. Press Download again to continue.")
        part.replace(target)
        state.update(status="done")
    except EngineError as error:
        state.update(status="failed", error=str(error))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        reason = getattr(error, "reason", error)
        state.update(
            status="failed",
            error=f"The download stopped ({reason}). Press Download again: it continues "
            "where it stopped.",
        )


def download(key: str, opener=None) -> dict:
    """Start downloading a model in the background (or say it is already there)."""
    if key not in CATALOGUE:
        raise EngineError("Unknown model.")
    with _lock:
        running = _downloads.get(key)
        if running and running["status"] in ("downloading", "checking"):
            return state()
        if model_path(key).is_file():
            return state()
        free = shutil.disk_usage(models_dir() if models_dir().exists() else Path.home()).free
        if free < CATALOGUE[key]["bytes"] * 1.1:
            raise EngineError(
                f"Not enough room on the disk: this model needs "
                f"{CATALOGUE[key]['bytes'] / 1e9:.1f} GB."
            )
        _downloads[key] = {
            "status": "downloading",
            "bytes": 0,
            "total": CATALOGUE[key]["bytes"],
            "error": None,
            "cancel": False,
        }
        thread = threading.Thread(target=_fetch, args=(key, opener), daemon=True)
        _downloads[key]["thread"] = thread
        thread.start()
    return state()


def cancel(key: str) -> dict:
    if key in _downloads:
        _downloads[key]["cancel"] = True
    return state()


def remove(key: str) -> dict:
    """Delete a model file (and stop the server that had it open)."""
    if key not in CATALOGUE:
        raise EngineError("Unknown model.")
    stop("vectors" if key == "vectors" else "writer")
    cancel(key)
    for path in (model_path(key), model_path(key).with_name(CATALOGUE[key]["file"] + ".part")):
        path.unlink(missing_ok=True)
    _downloads.pop(key, None)
    return state()


# ---- the servers --------------------------------------------------------------

_servers: dict[str, dict] = {}  # "writer" and "vectors"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _alive(server: dict | None) -> bool:
    return bool(server) and server["process"].poll() is None


def command(key: str, port: int) -> list[str]:
    """llama-server's arguments for one model. The key is not here: it goes in
    the environment, out of the list of running programs."""
    program = binary()
    if program is None:
        raise EngineError(
            "The built-in engine is not in this copy of the app. Install the desktop app, or "
            "connect a server in Settings ▸ Language models."
        )
    common = [str(program), "--model", str(model_path(key)), "--host", "127.0.0.1"]
    common += ["--port", str(port), "--no-webui", "--alias", CATALOGUE[key]["name"]]
    if key == "vectors":
        # An embedding is computed in one piece: the longest text it takes.
        return [*common, "--embeddings", "--parallel", "2", "--ctx-size", "4096"] + [
            "--batch-size", "2048", "--ubatch-size", "2048", "--n-gpu-layers", "99",
        ]  # fmt: skip
    return [*common, "--ctx-size", "8192", "--parallel", "1", "--n-gpu-layers", "99", "--jinja"]


def ensure(key: str, spawn=subprocess.Popen, wait: float = START_WAIT) -> dict:
    """The running server for this model, started if need be: ``{url, key, model}``."""
    if key not in CATALOGUE:
        raise EngineError("Unknown model.")
    role = "vectors" if key == "vectors" else "writer"
    with _lock:
        server = _servers.get(role)
        if _alive(server) and server["model"] == key:
            return server
        if not model_path(key).is_file():
            raise EngineError(
                f"The {CATALOGUE[key]['label'].lower()} model is not downloaded yet: "
                "Settings ▸ Language models ▸ On this computer."
            )
        _stop(role)
        port, secret = _free_port(), secrets.token_urlsafe(24)
        # Only this computer's own variables, never a LLAMA_* one: those set the
        # server's flags (one of them would write every prompt to disk).
        env = {  # nothing of ours that the engine has no use for
            k: v
            for k, v in os.environ.items()
            if not k.upper().startswith("LLAMA_") and k != settings.ENV_TOKEN
        }
        env["LLAMA_API_KEY"] = secret
        log = (models_dir() / f"{role}.log").open("ab")
        try:
            process = spawn(
                command(key, port),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )  # noqa: S603
        except OSError as error:
            raise EngineError(f"The built-in engine could not start ({error}).") from None
        server = {
            "process": process,
            "model": key,
            "url": f"http://127.0.0.1:{port}/v1",
            "health": f"http://127.0.0.1:{port}/health",
            "key": secret,
            "name": CATALOGUE[key]["name"],
            "started": time.time(),
        }
        _servers[role] = server
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if server["process"].poll() is not None:
            _servers.pop(role, None)
            raise EngineError(
                "The built-in engine stopped while loading the model. There may not be enough "
                "free memory: close other programs, or choose a lighter model."
            )
        try:
            with urllib.request.urlopen(server["health"], timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return server
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            pass
        time.sleep(0.4)
    stop(role)
    raise EngineError("The model took too long to load. Try a lighter one.")


def _stop(role: str) -> None:
    server = _servers.pop(role, None)
    if _alive(server):
        server["process"].terminate()
        try:
            server["process"].wait(timeout=8)
        except subprocess.TimeoutExpired:
            server["process"].kill()


def stop(role: str | None = None) -> None:
    with _lock:
        for name in [role] if role else list(_servers):
            _stop(name)


atexit.register(stop)


def state() -> dict:
    """Everything the page shows about the built-in engine."""
    program = binary()
    models = []
    for key, entry in CATALOGUE.items():
        running = _downloads.get(key)
        role = "vectors" if key == "vectors" else "writer"
        models.append(
            {
                "key": key,
                "label": entry["label"],
                "name": entry["name"],
                "gigabytes": round(entry["bytes"] / 1e9, 1),
                "memory": entry["memory"],
                "for": entry["for"],
                "downloaded": model_path(key).is_file(),
                "download": {k: running[k] for k in ("status", "bytes", "total", "error")}
                if running
                else None,
                "running": _alive(_servers.get(role)) and _servers[role]["model"] == key,
            }
        )
    return {
        "available": program is not None,
        "shipped": program is not None and program.parent == engine_dir(),
        "memory_gb": memory_gb(),
        "advised": advised(),
        "models": models,
        "folder": str(models_dir()),
    }
