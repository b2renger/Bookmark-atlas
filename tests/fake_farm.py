"""A stand-in for a LlmOnLan farm, for tests and for trying the page by hand.

It answers the routes Atlas uses, over real HTTP on the loopback: the farm's
description of itself, the model list, chat and text vectors. Its "model" is a
few lines of Python: enough to check what Atlas sends and what it does with
the answers, not to judge the quality of a real model.

    python tests/fake_farm.py            # serves on http://127.0.0.1:41997
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIMS = 48
CONTRACT = "embeddinggemma-2/768/v1"


def vector(text: str) -> list[float]:
    """Texts that share words get close vectors."""
    text = re.sub(r"^(task: [^|]*\| query: |title: [^|]*\| text: )", lambda m: m.group(0)[6:], text)
    out = [0.0] * DIMS
    for word in re.findall(r"\w{3,}", text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        out[digest[0] % DIMS] += 1.0 if digest[1] % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in out)) or 1.0
    return [v / norm for v in out]


class FakeFarm:
    def __init__(
        self, *, password=None, embeddings=True, busy=0, json_mode=True, port=0, provider=None,
        limit="max_completion_tokens", listed=None,
    ):  # fmt: skip
        # provider: None for a farm; "anthropic" for Claude's own API; "openai" for a
        # provider that refuses any parameter it does not know. `limit` is the one
        # name that provider takes for the length of an answer.
        self.provider = provider
        self.limit = limit
        self.listed = listed
        self.password = password
        self.embeddings = embeddings
        self.busy = busy  # how many chat requests to refuse first, as a full farm does
        self.json_mode = json_mode  # False: refuse `response_format`, as some servers do
        self.calls: list[dict] = []
        self.model = "gemma4:12b"
        farm = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, body, headers=None):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def _authorised(self, key):
                return key is None or self.headers.get("Authorization") == f"Bearer {key}"

            def do_GET(self):  # noqa: N802
                if self.path == "/lol/self":
                    return self._send(200, farm.snapshot())
                if self.path == "/lol/plugin-keys":
                    if not farm.password:
                        return self._send(404, {"error": "this farm has no password"})
                    if not self._authorised(farm.password):
                        return self._send(401, {"error": "unauthorized"})
                    return self._send(200, {"embed": "embed-key", "extract": None})
                if self.path.split("?")[0] in ("/v1/models", "/models"):
                    if farm.provider == "anthropic":
                        if not self._claude_headers():
                            return self._send(401, {"error": {"message": "invalid x-api-key"}})
                    elif not self._authorised(farm.password):
                        return self._send(401, {"error": {"message": "bad key"}})
                    names = farm.listed or [farm.model, "other:1b"]
                    return self._send(200, {"data": [{"id": name} for name in names]})
                self._send(404, {"error": {"message": "This farm does not serve that route."}})

            def _claude_headers(self):
                return (
                    self.headers.get("x-api-key") == farm.password
                    and self.headers.get("anthropic-version") == "2023-06-01"
                    and not self.headers.get("Authorization")
                )

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                farm.calls.append({"path": self.path, "body": body})
                if self.path == "/v1/messages" and farm.provider == "anthropic":
                    if not self._claude_headers():
                        return self._send(401, {"error": {"message": "invalid x-api-key"}})
                    if farm.busy > 0:
                        farm.busy -= 1
                        return self._send(
                            529, {"error": {"type": "overloaded_error", "message": "Overloaded"}}
                        )
                    wrong = set(body) - {"model", "max_tokens", "messages", "system"}
                    roles = {m["role"] for m in body.get("messages", [])}
                    if wrong or "system" in roles or not body.get("max_tokens"):
                        return self._send(
                            400,
                            {
                                "error": {
                                    "message": f"invalid request: {sorted(wrong)} {sorted(roles)}"
                                }
                            },
                        )
                    messages = [
                        {"role": "system", "content": body.get("system", "")},
                        *body["messages"],
                    ]
                    parts = [
                        {"type": "thinking", "thinking": "hmm"},
                        {"type": "text", "text": farm.reply(messages)},
                    ]
                    return self._send(200, {"content": parts, "stop_reason": "end_turn"})
                if self.path == "/v1/embeddings":
                    if not farm.embeddings:
                        return self._send(404, {"error": {"message": "no such route"}})
                    key = "embed-key" if farm.password else "open-embed-key"
                    if farm.provider:
                        key = farm.password
                    if not self._authorised(key):
                        return self._send(401, {"error": {"message": "bad key"}})
                    texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
                    rows = [{"index": i, "embedding": vector(t)} for i, t in enumerate(texts)]
                    return self._send(200, {"data": rows[::-1]})  # out of order, on purpose
                if self.path == "/v1/chat/completions":
                    if not self._authorised(farm.password):
                        return self._send(401, {"error": {"message": "bad key"}})
                    if farm.busy > 0:
                        farm.busy -= 1
                        return self._send(
                            429,
                            {
                                "error": {
                                    "message": "All 2 seats on this server are in use.",
                                    "code": "lol_seats_full",
                                }
                            },
                            {"Retry-After": str(farm.retry_after)},
                        )
                    if farm.provider == "openai":
                        other = {"max_tokens", "max_completion_tokens"} - {farm.limit}
                        allowed = {"model", "messages", "stream", "response_format", farm.limit}
                        for name in sorted(set(body) - allowed):
                            hint = f" Use '{farm.limit}' instead." if name in other else ""
                            message = f"Unsupported parameter: '{name}' is not supported with this model.{hint}"
                            return self._send(400, {"error": {"message": message}})
                    if "response_format" in body and not farm.json_mode:
                        return self._send(400, {"error": {"message": "response_format unknown"}})
                    text = farm.reply(body["messages"])
                    return self._send(200, {"choices": [{"message": {"content": text}}]})
                self._send(404, {"error": {"message": "This farm does not serve that route."}})

        self.retry_after = 1
        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def snapshot(self) -> dict:
        embed = None
        if self.embeddings:
            embed = {
                "url": f"http://10.9.9.9:{self.port}",  # the address the farm believes it has
                "key": None if self.password else "open-embed-key",
                "model": "embeddinggemma-2",
                "dims": 768,
                "contract": CONTRACT,
            }
        return {
            "v": 1,
            "id": "fake-farm",
            "name": "Stand-in farm",
            "proxyPort": self.port,
            "httpPort": self.port,
            "ips": ["10.9.9.9"],
            "openaiBaseUrl": f"http://10.9.9.9:{self.port}/v1",
            "requiresKey": bool(self.password),
            "models": [{"id": self.model, "default": True}],
            "healthy": True,
            "version": "0.0.52",
            "embed": embed,
        }

    def reply(self, messages: list[dict]) -> str:
        system = messages[0]["content"] if messages[0]["role"] == "system" else ""
        user = messages[-1]["content"]
        if "catalogue notes" in system:
            page = re.search(r"<<<PAGE\n(.*)\nPAGE>>>", user, re.S)
            text = page.group(1) if page else ""
            if "cookie policy" in text.lower():
                return '{"summary": "", "tags": []}'
            words = re.findall(r"[A-Za-zÀ-ÿ]{5,}", text)
            known = re.search(r"Tags already in use: (.*)", user)
            vocabulary = [t.strip() for t in (known.group(1) if known else "").split(",")]
            tags = [t for t in vocabulary if t and t.lower() in text.lower()][:3] + [
                "brand new tag"
            ]
            summary = "A page about " + ", ".join(dict.fromkeys(w.lower() for w in words[:4])) + "."
            return "```json\n" + json.dumps({"summary": f"**{summary}**", "tags": tags}) + "\n```"
        if "name the topics" in system:
            numbers = re.findall(r"Topic (\d+) \(", user)
            tags = re.findall(r"tags: ([^\n,]*)", user)
            names = {
                n: f"About {t.strip() or 'things'}" for n, t in zip(numbers, tags, strict=False)
            }
            return "<think>hmm</think>" + json.dumps({"names": names})
        if "tidy the tags" in system:
            tags = re.search(r"<<<TAGS\n(.*)\nTAGS>>>", user, re.S).group(1).split("\n")
            # "Synonyms", for the stand-in: tags that share their first four letters.
            families: dict[str, list[str]] = {}
            for tag in tags:
                families.setdefault(tag[:4].lower(), []).append(tag)
            groups = [g for g in families.values() if len(g) > 1]
            return json.dumps({"groups": [*groups, ["not a tag", "nor this"], [tags[0]]]})
        if "wrote a note about it" in system:
            note = re.search(r"Note then: (.*)", user).group(1).lower()
            page = re.search(r"<<<PAGE\n(.*)\nPAGE>>>", user, re.S).group(1).lower()
            if "domain is for sale" in page:
                return '{"verdict": "replaced", "note": "The domain is now parked and for sale."}'
            shared = [w for w in re.findall(r"[a-z]{6,}", note) if w in page]
            if shared:
                return '{"verdict": "same", "note": "should be dropped"}'
            return (
                '{"verdict": "changed", "note": "The page no longer mentions what the note says."}'
            )
        if "research assistant" in system:
            question = user.rsplit("Question: ", 1)[-1].lower()
            blocks = re.findall(
                r"\[(\d+)\] from “[^\n]*\n(.*?)(?=\n\n\[\d+\] from “|\nPASSAGES>>>)", user, re.S
            )
            asked = set(re.findall(r"[a-zà-ÿ]{5,}", question))
            statements = []
            for number, text in blocks:
                if asked & set(re.findall(r"[a-zà-ÿ]{5,}", text.lower())):
                    words = text.split()
                    statements.append(
                        {
                            "text": f"Passage {number} speaks of this.",
                            "passage": int(number),
                            "quote": " ".join(words[:8]).upper(),
                        }
                    )
            if "invent" in question:  # a model that makes things up: these must not get through
                statements.append(
                    {
                        "text": "The moon is made of cheese.",
                        "passage": 1,
                        "quote": "the moon is made entirely of green cheese",
                    }
                )
                statements.append(
                    {"text": "No such passage.", "passage": 99, "quote": "nothing at all here"}
                )
            missing = "" if statements else "The passages do not speak of this."
            return json.dumps({"statements": statements[:6], "missing": missing})
        if "Rewrite the last question" in system:
            last = user.rsplit("Last question: ", 1)[-1]
            first = re.search(r"user: (.*)", user).group(1)
            return json.dumps({"question": f"{last} (about: {first})"})
        if "single word: ready" in user:
            return "ready"
        numbers = re.findall(r"^\[(\d+)\]", user, re.M) or re.findall(r"\[(\d+)\]", user)
        cited = " ".join(f"[{n}]" for n in dict.fromkeys(numbers[:3]))
        if "This is one part of a larger set" in user:
            return f"- notes on {cited}"
        return f"These bookmarks share one thread {cited}. [999] is not in the list."

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def config(self) -> dict:
        return {
            "kind": "farm",
            "name": "Stand-in farm",
            "base_url": f"{self.url}/v1",
            "self_url": f"{self.url}/lol/self",
            "chat_model": self.model,
        }

    def close(self):
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    import time

    made = FakeFarm(port=41997)
    print(f"Stand-in farm on {made.url} (add it in Settings with the address 127.0.0.1)")
    while True:
        time.sleep(3600)
