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


def load() -> dict:
    """The connection as saved: ``{}`` when none was set up."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(config: dict) -> dict:
    keep = {
        key: config[key]
        for key in (
            "kind",
            "name",
            "base_url",
            "self_url",
            "chat_model",
            "embed_url",
            "embed_model",
        )
        if config.get(key)
    }
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keep, indent=2), encoding="utf-8")
    return keep


def forget() -> None:
    _path().unlink(missing_ok=True)
    settings.delete_secret(SECRET)


def describe() -> dict:
    """The connection for the page: never the password itself."""
    config = load()
    return {
        "connected": bool(config.get("base_url")),
        "kind": config.get("kind"),
        "name": config.get("name"),
        "base_url": config.get("base_url"),
        "chat_model": config.get("chat_model"),
        "embed_url": config.get("embed_url"),
        "embed_model": config.get("embed_model"),
        "has_password": bool(settings.get_secret(SECRET)),
        "local": is_local(config.get("base_url") or ""),
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


def request(method: str, url: str, *, key: str | None = None, body=None, timeout: int = TIMEOUT):
    """One JSON request. Returns the decoded answer; raises ``Busy`` or ``LlmError``
    with the server's own words when it refuses."""
    headers = {"Accept": "application/json", "User-Agent": "BookmarkAtlas"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
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
        if error.code in (429, 503):
            try:
                wait = int(error.headers.get("Retry-After") or 60)
            except ValueError:
                wait = 60
            raise Busy(message or "The server is busy.", wait) from None
        if error.code in (401, 403):
            raise LlmError("The server did not accept the password.") from None
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

    def __init__(self, config: dict | None = None, password: str | None = None):
        config = load() if config is None else config
        if not config.get("base_url"):
            raise LlmError(
                "No language model is connected. Connect one in Settings ▸ Language models."
            )
        self.config = config
        self.base = config["base_url"].rstrip("/")
        self.password = settings.get_secret(SECRET) if password is None else (password or None)
        self.chat_model = config.get("chat_model")
        self._embed = None
        self._json_mode = True

    # -- chat --

    def models(self) -> list[str]:
        answer = request("GET", f"{self.base}/models", key=self.password, timeout=15)
        return [m.get("id") for m in (answer or {}).get("data") or [] if m.get("id")]

    def chat(
        self,
        messages: list[dict],
        *,
        want_json: bool = False,
        max_tokens: int = 900,
        temperature: float = 0.2,
        waits: int = 2,
    ) -> str:
        """The model's reply as text. A full server is waited for a little, then
        ``Busy`` is raised so a long job can stop and say when to come back."""
        if not self.chat_model:
            raise LlmError("No model is chosen. Pick one in Settings ▸ Language models.")
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
            if self._json_mode:
                body["response_format"] = {"type": "json_object"}
        for attempt in range(waits + 1):
            try:
                answer = request(
                    "POST", f"{self.base}/chat/completions", key=self.password, body=body
                )
                break
            except Busy as busy:
                if attempt == waits or busy.retry_after > 20:
                    raise
                time.sleep(busy.retry_after)
            except LlmError as error:
                if "response_format" in body and re.search(r" 4(00|22):", str(error)):
                    self._json_mode = False  # this server does not take it: ask in words only
                    del body["response_format"]
                    continue
                raise
        else:
            raise LlmError("The server kept refusing the request.")
        try:
            message = answer["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LlmError("The model's answer had an unexpected shape.") from None
        text = message.get("content") or ""
        if isinstance(text, list):  # some servers answer in parts
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        return strip_thinking(text)

    def ask_json(self, messages: list[dict], **options):
        """A reply that must be JSON: parsed, or ``LlmError``."""
        return parse_json(self.chat(messages, want_json=True, **options))

    # -- vectors --

    def embedding(self) -> dict:
        """Where vectors come from: ``{url, key, model, contract, dims}``."""
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
    result = {
        "models": models,
        "chat_model": connection.chat_model,
        "reply": reply[:80],
        "seconds": seconds,
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
