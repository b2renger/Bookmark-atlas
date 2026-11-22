"""Talking to a language model on your own network.

Atlas speaks the OpenAI-compatible HTTP API (``/v1/chat/completions``,
``/v1/models``, ``/v1/embeddings``), so any server that answers it will do. It
knows one family of servers better: a **LlmOnLan farm**
(https://github.com/b2renger/LlmOnLan), which announces itself on the local
network, describes itself at ``GET http://<box>:41997/lol/self`` and may offer
text vectors (its "document search" service) next to chat.

What is sent where is decided by the person: nothing here runs unless they
connected a server in Settings. The address and model names are kept in a
small settings file; the password goes to the system keychain, like the
Notion token, and never into a database, a snapshot or an export.
"""

from __future__ import annotations

import json
import re
import socket
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import settings

BEACON_GROUP = "239.255.43.10"  # LlmOnLan's discovery group and ports
BEACON_PORT = 41998
SELF_PORT = 41997
SECRET = "llm-key"
TIMEOUT = 180
# What a farm's vectors are made with; the client adds the prefixes.
CONTRACTS = {
    "embeddinggemma-2/768/v1": {
        "model": "embeddinggemma-2",
        "dims": 768,
        "query": "task: search result | query: ",
        "document": "title: {title} | text: ",
    }
}


# Providers reached over the internet with an API key. ``api`` is the shape of
# their chat endpoint; ``limit`` the name their newest models take for the
# length of an answer (the other spelling is tried when one is refused).
PROVIDERS = {
    "anthropic": {
        "name": "Claude (Anthropic)",
        "base_url": "https://api.anthropic.com/v1",
        "api": "anthropic",
        "keys": "console.anthropic.com ▸ API keys",
        "prefer": ("sonnet", "haiku"),
        "vectors": (),
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api": "openai",
        "limit": "max_completion_tokens",
        "keys": "platform.openai.com ▸ API keys",
        "prefer": ("-mini", "gpt"),
        "vectors": ("text-embedding-3-small", "embedding"),
    },
    "gemini": {
        "name": "Gemini (Google)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api": "openai",
        "limit": "max_tokens",
        "keys": "aistudio.google.com ▸ Get API key",
        "prefer": ("flash", "gemini"),
        "vectors": ("gemini-embedding", "embedding"),
    },
    "mistral": {
        "name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "api": "openai",
        "limit": "max_tokens",
        "keys": "console.mistral.ai ▸ API keys",
        "prefer": ("small", "mistral"),
        "vectors": ("mistral-embed", "embed"),
    },
}
ANTHROPIC_VERSION = "2023-06-01"
# Listed by a provider beside its chat models, and of no use for writing text.
_NOT_FOR_CHAT = (
    "embed", "tts", "whisper", "audio", "image", "imagen", "dall-e", "moderation", "realtime",
    "transcribe", "sora", "veo", "rerank", "ocr", "aqa", "learnlm", "computer-use", "robotics",
)  # fmt: skip


class LlmError(RuntimeError):
    """Something the person can act on: no server, wrong password, a refusal."""


class Busy(LlmError):
    """The server has no room right now; ``retry_after`` seconds is its estimate."""

    def __init__(self, message: str, retry_after: int = 60):
        super().__init__(message)
        self.retry_after = retry_after


# ---- settings ---------------------------------------------------------------


def _path():
    return settings.config_dir() / "llm.json"


_FIELDS = ("kind", "name", "base_url", "self_url", "chat_model", "embed_url", "embed_model")


def slot_of(config: dict) -> str:
    """Where a connection is kept: one place for this computer, one for the
    local network, one for each provider."""
    kind = config.get("kind") or ""
    return kind if kind == "local" or kind in PROVIDERS else "lan"


def _read() -> dict:
    """The file as ``{"active": slot or None, "saved": {slot: connection}}``.
    A file from before several connections could be kept holds one connection."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"active": None, "saved": {}}
    if not isinstance(data, dict):
        return {"active": None, "saved": {}}
    if "saved" not in data:
        if not data.get("base_url"):
            return {"active": None, "saved": {}}
        only = _settled(data)
        return {"active": slot_of(only), "saved": {slot_of(only): only}}
    saved = {k: _settled(v) for k, v in (data.get("saved") or {}).items() if isinstance(v, dict)}
    active = data.get("active")
    return {"active": active if active in saved else None, "saved": saved}


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load(slot: str | None = None) -> dict:
    """The connection in use (or the one kept in ``slot``): ``{}`` when none."""
    data = _read()
    return data["saved"].get(slot or data["active"] or "", {})


def _settled(config: dict) -> dict:
    """A provider's name on a connection only counts with that provider's own
    address. (Before providers existed, a server reached by its address was
    saved under the kind "openai": it is a plain server.)"""
    provider = PROVIDERS.get(config.get("kind") or "")
    if provider and (config.get("base_url") or "").rstrip("/") != provider["base_url"]:
        return {**config, "kind": "server"}
    return config


def save(config: dict) -> dict:
    """Keep a connection in its place and make it the one in use. The others
    stay, so a question can be sent to any of them."""
    keep = {key: config[key] for key in _FIELDS if config.get(key)}
    data = _read()
    data["saved"][slot_of(keep)] = keep
    data["active"] = slot_of(keep)
    _write(data)
    return keep


def secret_name(slot: str) -> str:
    return f"{SECRET}:{slot}"


def secret_for(slot: str) -> str | None:
    """The password or key of a kept connection. One kept before each
    connection had its own is still found."""
    return settings.get_secret(secret_name(slot)) or (
        settings.get_secret(SECRET) if slot == _read()["active"] else None
    )


def activate(slot: str) -> dict:
    data = _read()
    if slot not in data["saved"]:
        raise LlmError("That connection is not kept any more.")
    legacy = settings.get_secret(SECRET)
    if legacy and data["active"] and not settings.get_secret(secret_name(data["active"])):
        settings.set_secret(secret_name(data["active"]), legacy)  # give the old one its place
    if legacy:
        settings.delete_secret(SECRET)
    data["active"] = slot
    _write(data)
    return data["saved"][slot]


def forget(slot: str | None = None) -> None:
    """Forget a kept connection (the one in use when none is named) and its key."""
    data = _read()
    slot = slot or data["active"]
    if not slot:
        return
    data["saved"].pop(slot, None)
    settings.delete_secret(secret_name(slot))
    if data["active"] == slot:
        settings.delete_secret(SECRET)
        data["active"] = None
    _write(data)


def _card(slot: str, config: dict, active: str | None) -> dict:
    provider = PROVIDERS.get(config.get("kind") or "", {}).get("name")
    local = config.get("kind") == "local" or is_local(config.get("base_url") or "")
    if config.get("kind") == "local":
        label = f"This computer ({config.get('chat_model')})"
    else:
        label = f"{provider or config.get('name') or 'My server'} ({config.get('chat_model')})"
    return {
        "slot": slot,
        "label": label,
        "kind": config.get("kind"),
        "chat_model": config.get("chat_model"),
        "provider": provider,
        "local": local,
        "active": slot == active,
    }


def kept() -> list[dict]:
    """Every connection kept, the one in use first: what a question can be sent to."""
    data = _read()
    cards = [_card(slot, config, data["active"]) for slot, config in data["saved"].items()]
    return sorted(cards, key=lambda card: (not card["active"], card["label"]))


def describe() -> dict:
    """The connection for the page: never the password itself."""
    data = _read()
    config = data["saved"].get(data["active"] or "", {})
    return {
        "connected": bool(config.get("base_url")),
        "kind": config.get("kind"),
        "name": config.get("name"),
        "base_url": config.get("base_url"),
        "chat_model": config.get("chat_model"),
        "embed_url": config.get("embed_url"),
        "embed_model": config.get("embed_model"),
        "has_password": bool(data["active"] and secret_for(data["active"])),
        "local": config.get("kind") == "local" or is_local(config.get("base_url") or ""),
        "provider": PROVIDERS.get(config.get("kind") or "", {}).get("name"),
        "slot": data["active"],
    }


def is_local(url: str) -> bool:
    """Does this address stay on the machine or the local network?"""
    host = (re.match(r"https?://\[?([^/\]:]+)", url or "") or [None, ""])[1].lower()
    if host in ("localhost", "") or host.endswith((".local", ".lan", ".home")):
        return bool(host)
    try:
        parts = [int(part) for part in host.split(".")]
    except ValueError:
        return "." not in host  # a bare machine name
    return len(parts) == 4 and (
        parts[0] in (10, 127)
        or (parts[0] == 192 and parts[1] == 168)
        or (parts[0] == 172 and 16 <= parts[1] <= 31)
        or (parts[0] == 169 and parts[1] == 254)
    )


# ---- HTTP ---------------------------------------------------------------------


def request(
    method: str,
    url: str,
    *,
    key: str | None = None,
    body=None,
    timeout: int = TIMEOUT,
    headers: dict | None = None,
):
    """One JSON request. Returns the decoded answer; raises ``Busy`` or ``LlmError``
    with the server's own words when it refuses. ``headers`` replace the usual
    ``Authorization: Bearer`` for a provider that names its key otherwise."""
    extra = headers
    headers = {"Accept": "application/json", "User-Agent": "BookmarkAtlas"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if extra is not None:
        headers.update(extra)
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    call = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(call, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8", "replace") or "null")
    except urllib.error.HTTPError as error:
        text = error.read(20_000).decode("utf-8", "replace")
        try:
            detail = json.loads(text).get("error")
            message = detail.get("message") if isinstance(detail, dict) else str(detail or text)
        except (ValueError, AttributeError):
            message = text.strip()[:300]
        if error.code in (429, 503, 529):  # 529: Anthropic's "overloaded"
            try:
                wait = int(error.headers.get("Retry-After") or 60)
            except ValueError:
                wait = 60
            raise Busy(message or "The server is busy.", wait) from None
        if error.code in (401, 403):
            raise LlmError("The server did not accept the password or API key.") from None
        raise LlmError(f"The server answered {error.code}: {message or 'no detail'}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise LlmError(f"The server at {url.split('/v1')[0]} did not answer ({reason}).") from None
    except ValueError:
        raise LlmError("The server's answer could not be read.") from None


# ---- finding a farm -----------------------------------------------------------


def _farm_card(snapshot: dict, seen_from: str = "") -> dict | None:
    """What Atlas needs from a farm's description of itself."""
    if not isinstance(snapshot, dict) or not snapshot.get("openaiBaseUrl"):
        return None
    host = seen_from or (snapshot.get("ips") or [""])[0]
    port = snapshot.get("proxyPort") or 4000
    models = [m.get("id") for m in snapshot.get("models") or [] if isinstance(m, dict)]
    default = next(
        (m.get("id") for m in snapshot.get("models") or [] if m.get("default")),
        models[0] if models else None,
    )
    embed = snapshot.get("embed") if isinstance(snapshot.get("embed"), dict) else None
    return {
        "kind": "farm",
        "id": snapshot.get("id"),
        "name": snapshot.get("name") or "LlmOnLan farm",
        "base_url": f"http://{host}:{port}/v1" if host else snapshot["openaiBaseUrl"],
        "self_url": f"http://{host}:{snapshot.get('httpPort') or SELF_PORT}/lol/self",
        "requires_password": bool(snapshot.get("requiresKey")),
        "healthy": bool(snapshot.get("healthy", True)),
        "models": models,
        "chat_model": default,
        "embeddings": bool(embed and embed.get("contract") in CONTRACTS),
        "embed_contract": embed.get("contract") if embed else None,
        "version": snapshot.get("version"),
    }


def read_self(host: str, port: int = SELF_PORT) -> dict:
    """Ask one machine whether it is a farm."""
    host, _, given = re.sub(r"^https?://", "", host.strip()).split("/")[0].partition(":")
    if not host:
        raise LlmError("Give the machine's name or address.")
    if given.isdigit() and int(given) not in (
        4000,
    ):  # the chat port is not where a farm describes itself
        port = int(given)
    card = _farm_card(request("GET", f"http://{host}:{port}/lol/self", timeout=5), host)
    if not card:
        raise LlmError(f"{host} answered, but not as a LlmOnLan farm.")
    return card


def discover(seconds: float = 6.0, port: int = BEACON_PORT) -> list[dict]:
    """Listen for the farms announcing themselves on the local network."""
    found: dict[str, dict] = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
    except OSError as error:
        raise LlmError(f"Could not listen for farms on this machine ({error}).") from None
    try:
        try:
            member = struct.pack("4sl", socket.inet_aton(BEACON_GROUP), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, member)
        except OSError:
            pass  # broadcast still reaches us
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            sock.settimeout(max(0.05, end - time.monotonic()))
            try:
                data, (address, _) = sock.recvfrom(65535)
            except (TimeoutError, OSError):
                continue
            try:
                card = _farm_card(json.loads(data.decode("utf-8", "replace")), address)
            except ValueError:
                continue
            if card:
                found[card.get("id") or address] = card
    finally:
        sock.close()
    return sorted(found.values(), key=lambda card: card["name"])


# ---- the connection in use ----------------------------------------------------


class Connection:
    """The saved connection, resolved: where chat and vectors are asked for."""

    def __init__(
        self, config: dict | None = None, password: str | None = None, slot: str | None = None
    ):
        if config is None and slot:
            config = load(slot)
            if not config:
                raise LlmError("That connection is not kept any more: choose another.")
            password = secret_for(slot) or "" if password is None else password
        elif config is None and password is None:
            active = _read()["active"]
            password = (secret_for(active) or "") if active else None
        config = load() if config is None else _settled(config)
        if not config.get("base_url"):
            raise LlmError(
                "No language model is connected. Connect one in Settings ▸ Language models."
            )
        self.config = config
        self.base = config["base_url"].rstrip("/")
        self.password = settings.get_secret(SECRET) if password is None else (password or None)
        self.chat_model = config.get("chat_model")
        # The model inside the app: its server is started when first needed.
        self.built_in = config.get("chat_model") if config.get("kind") == "local" else None
        self.provider = PROVIDERS.get(config.get("kind") or "")
        self.api = self.provider["api"] if self.provider else "local"
        self._limit = self.provider.get("limit", "max_tokens") if self.provider else "max_tokens"
        self._embed = None
        self._json_mode = True

    def _auth(self) -> dict | None:
        """The headers a provider wants in place of ``Authorization: Bearer``."""
        if self.api != "anthropic":
            return None
        return {"x-api-key": self.password or "", "anthropic-version": ANTHROPIC_VERSION}

    # -- chat --

    def _start(self) -> None:
        """Have the built-in engine running with the chosen model."""
        if not self.built_in:
            return
        from . import engine

        try:
            server = engine.ensure(self.built_in)
        except engine.EngineError as error:
            raise LlmError(str(error)) from None
        self.base, self.password, self.chat_model = server["url"], server["key"], server["name"]

    def all_models(self) -> list[str]:
        """Every model the server lists, as the names it takes."""
        if self.built_in:
            from . import engine

            return [key for key in engine.WRITERS if engine.model_path(key).is_file()]
        url = f"{self.base}/models" + ("?limit=1000" if self.api == "anthropic" else "")
        answer = request("GET", url, key=self.password, timeout=20, headers=self._auth())
        names = [m.get("id") for m in (answer or {}).get("data") or [] if m.get("id")]
        return [name.removeprefix("models/") for name in names]

    def models(self) -> list[str]:
        """The models that write text, the likeliest choice first for a provider."""
        names = self.all_models()
        if not self.provider:
            return names
        names = [n for n in names if not any(word in n.lower() for word in _NOT_FOR_CHAT)]
        prefer = self.provider["prefer"]
        rank = lambda n: next((i for i, w in enumerate(prefer) if w in n.lower()), len(prefer))  # noqa: E731
        return sorted(names, key=lambda n: (rank(n), n))

    def vector_models(self) -> list[str]:
        """The provider's models that turn text into vectors, likeliest first."""
        if not self.provider or not self.provider["vectors"]:
            return []
        prefer = self.provider["vectors"]
        names = [n for n in self.all_models() if "embed" in n.lower()]
        rank = lambda n: next((i for i, w in enumerate(prefer) if w in n.lower()), len(prefer))  # noqa: E731
        return sorted(names, key=lambda n: (rank(n), n))

    def chat(
        self,
        messages: list[dict],
        *,
        want_json: bool = False,
        max_tokens: int = 900,
        temperature: float = 0.2,
        waits: int = 2,
        schema: dict | None = None,
    ) -> str:
        """The model's reply as text. A full server is waited for a little, then
        ``Busy`` is raised so a long job can stop and say when to come back.
        ``schema`` (a JSON schema) holds a local model to the form asked for."""
        if not self.chat_model:
            raise LlmError("No model is chosen. Pick one in Settings ▸ Language models.")
        self._start()
        url = f"{self.base}/chat/completions"
        if self.api == "anthropic":
            # Claude's own API: the instructions apart from the conversation.
            url = f"{self.base}/messages"
            system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
            body = {
                "model": self.chat_model,
                "max_tokens": max_tokens,
                "messages": [m for m in messages if m["role"] != "system"],
            }
            if system:
                body["system"] = system
        elif self.api == "openai":
            # A provider refuses parameters it does not know, and its newest
            # models think before they answer: that thinking is counted in the
            # length allowed, so more room is given than the answer needs.
            body = {"model": self.chat_model, "messages": messages, "stream": False}
            body[self._limit] = max_tokens * 4 + 1000
            if want_json and self._json_mode:
                body["response_format"] = {"type": "json_object"}
        else:
            body = {
                "model": self.chat_model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            }
            if want_json:
                # Short structured answers do not need the model to think aloud first
                # (the farm passes both spellings through to whichever engine serves).
                body["chat_template_kwargs"] = {"enable_thinking": False}
                body["think"] = False
                if self._json_mode and schema and self.built_in:
                    # llama.cpp can only produce text that fits the schema: a small
                    # model cannot wander off the form.
                    body["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {"name": "answer", "schema": schema},
                    }
                elif self._json_mode:
                    body["response_format"] = {"type": "json_object"}
        for attempt in range(waits + 3):
            try:
                answer = request(
                    "POST",
                    url,
                    key=self.password,
                    body=body,
                    headers=self._auth(),
                    timeout=900 if self.built_in else TIMEOUT,  # no graphics card: be patient
                )
                break
            except Busy as busy:
                if attempt >= waits or busy.retry_after > 20:
                    raise
                time.sleep(busy.retry_after)
            except LlmError as error:
                refused = re.search(r" 4(00|22):", str(error))
                other = (
                    "max_tokens"
                    if self._limit == "max_completion_tokens"
                    else "max_completion_tokens"
                )
                if (
                    refused
                    and self.api == "openai"
                    and self._limit in body
                    and self._limit in str(error)
                ):
                    body[other] = body.pop(self._limit)  # this model wants the other spelling
                    self._limit = other
                    continue
                if refused and "response_format" in body:
                    self._json_mode = False  # this server does not take it: ask in words only
                    del body["response_format"]
                    continue
                raise
        else:
            raise LlmError("The server kept refusing the request.")
        try:
            if self.api == "anthropic":
                parts = answer["content"]
                text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            else:
                text = answer["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LlmError("The model's answer had an unexpected shape.") from None
        if isinstance(text, list):  # some servers answer in parts
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        return strip_thinking(text)

    def ask_json(self, messages: list[dict], **options):
        """A reply that must be JSON: parsed, or ``LlmError``."""
        return parse_json(self.chat(messages, want_json=True, **options))

    # -- vectors --

    def embedding(self) -> dict:
        """Where vectors come from: ``{url, key, model, contract, dims}``."""
        if self.built_in:
            from . import engine

            try:
                server = engine.ensure("vectors")
            except engine.EngineError as error:
                raise LlmError(str(error)) from None
            return {
                "url": f"{server['url']}/embeddings",
                "key": server["key"],
                "model": server["name"],
                "contract": engine.VECTOR_CONTRACT,
                "dims": 768,
            }
        if self._embed:
            return self._embed
        config = self.config
        if config.get("kind") == "farm" and config.get("self_url"):
            snapshot = request("GET", config["self_url"], timeout=8)
            embed = snapshot.get("embed") if isinstance(snapshot, dict) else None
            if not embed or embed.get("contract") not in CONTRACTS:
                raise LlmError(
                    "This farm does not offer text vectors right now. Its operator can turn "
                    "“Document search” on in the farm panel."
                )
            key = embed.get("key")
            if not key and snapshot.get("requiresKey"):
                keys = request(
                    "GET",
                    config["self_url"].replace("/lol/self", "/lol/plugin-keys"),
                    key=self.password,
                    timeout=8,
                )
                key = (keys or {}).get("embed")
                key = key.get("key") if isinstance(key, dict) else key
            host = re.match(r"https?://([^/:]+)", config["self_url"])[1]
            url = re.sub(r"(https?://)[^/:]+", rf"\g<1>{host}", embed["url"].rstrip("/"))
            contract = CONTRACTS[embed["contract"]]
            self._embed = {
                "url": f"{url}/v1/embeddings",
                "key": key,
                "model": embed.get("model") or contract["model"],
                "contract": embed["contract"],
                "dims": contract["dims"],
            }
        elif self.api == "anthropic":
            raise LlmError(
                "Claude writes text but does not make text vectors. The map and topics keep "
                "comparing words; everything else works. For “meaning”, connect a provider or "
                "a server that offers vectors."
            )
        elif config.get("embed_model"):
            base = (config.get("embed_url") or self.base).rstrip("/")
            model = config["embed_model"]
            self._embed = {
                "url": f"{base}/embeddings",
                "key": self.password,
                "model": model,
                "contract": next(
                    (name for name, c in CONTRACTS.items() if c["model"] in model), f"{model}/v1"
                ),
                "dims": None,
            }
        else:
            raise LlmError(
                "No model for text vectors is set. Name one in Settings ▸ Language models."
            )
        return self._embed

    def embed(self, texts: list[str]) -> list[list[float]]:
        """One vector per text, in order."""
        where = self.embedding()
        answer = request(
            "POST", where["url"], key=where["key"], body={"model": where["model"], "input": texts}
        )
        try:
            rows = sorted(answer["data"], key=lambda row: row.get("index", 0))
            vectors = [row["embedding"] for row in rows]
        except (KeyError, TypeError):
            raise LlmError("The vectors came back in an unexpected shape.") from None
        if len(vectors) != len(texts):
            raise LlmError("The server returned fewer vectors than texts.")
        return vectors

    def embed_many(self, texts: list[str], batch: int = 16, workers: int = 3):
        parts = [texts[i : i + batch] for i in range(0, len(texts), batch)]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return [vector for part in pool.map(self.embed, parts) for vector in part]


def document_text(contract: str, title: str | None, body: str) -> str:
    """A bookmark as the vector model expects a document."""
    shape = CONTRACTS.get(contract)
    title = " ".join((title or "").split())
    if not shape:
        return f"{title}\n{body}".strip()
    return shape["document"].format(title=title or "none") + body


def query_text(contract: str, question: str) -> str:
    shape = CONTRACTS.get(contract)
    return (shape["query"] if shape else "") + question


# ---- reading what a model wrote -----------------------------------------------


def strip_thinking(text: str) -> str:
    """Drop a model's reasoning-aloud block, when it left one in the answer."""
    text = re.sub(r"<think(ing)?>.*?</think(ing)?>", "", text or "", flags=re.S | re.I)
    return re.sub(r"^.*?</think(ing)?>", "", text, flags=re.S | re.I).strip()


def parse_json(text: str):
    """The JSON object or list in a reply, however the model wrapped it."""
    text = strip_thinking(text)
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, flags=re.S)
    for candidate in ([fenced.group(1)] if fenced else []) + [text]:
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except ValueError:
            pass
        start = min((i for i in (candidate.find("{"), candidate.find("[")) if i >= 0), default=-1)
        end = max(candidate.rfind("}"), candidate.rfind("]"))
        if 0 <= start < end:
            try:
                return json.loads(candidate[start : end + 1])
            except ValueError:
                pass
    raise LlmError("The model did not answer in the form that was asked for.")


def test(config: dict, password: str | None) -> dict:
    """Try a connection before saving it: the model list, one short reply, and
    one vector when vectors are offered."""
    connection = Connection(config, password=password or "")
    models = connection.models()
    if not connection.chat_model and models:
        connection.chat_model = models[0]
    started = time.monotonic()
    reply = connection.chat(
        [{"role": "user", "content": "Answer with the single word: ready"}], max_tokens=200
    )
    seconds = round(time.monotonic() - started, 1)
    vector_models = connection.vector_models()
    if vector_models and not config.get("embed_model"):
        connection.config = {**connection.config, "embed_model": vector_models[0]}
    result = {
        "models": models,
        "chat_model": connection.chat_model,
        "reply": reply[:80],
        "seconds": seconds,
        "vector_models": vector_models,
        "embed_model": connection.config.get("embed_model"),
        "vectors": None,
        "vectors_note": None,
    }
    try:
        where = connection.embedding()
        vector = connection.embed([query_text(where["contract"], "ready")])[0]
        result["vectors"] = {"model": where["model"], "dims": len(vector)}
    except LlmError as error:
        result["vectors_note"] = str(error)
    return result


def providers() -> list[dict]:
    """The providers the page offers, without anything secret."""
    return [
        {
            "kind": kind,
            "name": p["name"],
            "base_url": p["base_url"],
            "keys": p["keys"],
            "vectors": bool(p["vectors"]),
        }
        for kind, p in PROVIDERS.items()
    ]
