"""An in-memory stand-in for the Notion API, shaped like the real answers.

It implements only the endpoints Atlas calls. Response shapes follow the
Notion reference as read on 2026-11-11 (API version 2026-03-11). It is not a
substitute for testing against the real service.
"""

from __future__ import annotations

import json
from datetime import datetime

VALID_TOKEN = "ntn_test_token_0000000000"

SCHEMA = {
    "Name": {"id": "title", "name": "Name", "type": "title", "title": {}},
    "URL": {"id": "u1", "name": "URL", "type": "url", "url": {}},
    "Description": {"id": "d1", "name": "Description", "type": "rich_text", "rich_text": {}},
    "Keywords": {"id": "k1", "name": "Keywords", "type": "multi_select", "multi_select": {}},
    "Date": {"id": "t1", "name": "Date", "type": "date", "date": {}},
    "Platform": {"id": "p1", "name": "Platform", "type": "select", "select": {}},
}


def rich(text: str) -> list:
    if not text:
        return []
    return [{"type": "text", "text": {"content": text, "link": None}, "plain_text": text}]


def make_page(
    page_id,
    title,
    url,
    *,
    summary="",
    tags=(),
    date=None,
    created="2026-06-01T10:00:00.000Z",
    edited=None,
    in_trash=False,
):
    return {
        "object": "page",
        "id": page_id,
        "created_time": created,
        "last_edited_time": edited or created,
        "in_trash": in_trash,
        "properties": {
            "Name": {"id": "title", "type": "title", "title": rich(title)},
            "URL": {"id": "u1", "type": "url", "url": url},
            "Description": {"id": "d1", "type": "rich_text", "rich_text": rich(summary)},
            "Keywords": {
                "id": "k1",
                "type": "multi_select",
                "multi_select": [{"name": tag} for tag in tags],
            },
            "Date": {
                "id": "t1",
                "type": "date",
                "date": {"start": date, "end": None, "time_zone": None} if date else None,
            },
        },
    }


def _when(text):
    return datetime.fromisoformat(text)


class FakeNotion:
    """Use ``fake.transport`` as the client's transport."""

    def __init__(self, page_size=2):
        self.page_size = page_size  # small, so pagination is always exercised
        self.tables: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail_next: list[tuple[int, dict]] = []  # (status, headers) answered first
        self.incomplete = False

    def add_table(self, table_id, title, pages=(), schema=None, database_id="db-1"):
        self.tables[table_id] = {
            "title": title,
            "schema": schema or SCHEMA,
            "pages": list(pages),
            "database_id": database_id,
        }

    def _data_source(self, table_id):
        table = self.tables[table_id]
        return {
            "object": "data_source",
            "id": table_id,
            "title": rich(table["title"]),
            "parent": {"type": "database_id", "database_id": table["database_id"]},
            "properties": table["schema"],
            "in_trash": False,
            "last_edited_time": "2026-11-01T00:00:00.000Z",
        }

    def _paginate(self, items, body):
        start = int(body.get("start_cursor") or 0)
        chunk = items[start : start + self.page_size]
        more = start + self.page_size < len(items)
        answer = {
            "object": "list",
            "results": chunk,
            "has_more": more,
            "next_cursor": str(start + self.page_size) if more else None,
        }
        if not more and self.incomplete:
            answer["request_status"] = {
                "type": "incomplete",
                "incomplete_reason": "query_result_limit_reached",
            }
        return answer

    def transport(self, method, url, headers, body):
        path = url.split("/v1", 1)[1]
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload))

        def answer(status, data, extra=None):
            return status, extra or {}, json.dumps(data).encode()

        def error(status, code, message):
            return answer(
                status, {"object": "error", "status": status, "code": code, "message": message}
            )

        if self.fail_next:
            status, extra = self.fail_next.pop(0)
            return answer(
                status,
                {
                    "object": "error",
                    "status": status,
                    "code": "rate_limited",
                    "message": "slow down",
                },
                extra,
            )
        if headers.get("Authorization") != f"Bearer {VALID_TOKEN}":
            return error(401, "unauthorized", "API token is invalid.")
        if not headers.get("Notion-Version"):
            return error(400, "missing_version", "Notion-Version header is required.")

        if (method, path) == ("GET", "/users/me"):
            return answer(
                200,
                {
                    "object": "user",
                    "type": "bot",
                    "name": "Atlas",
                    "bot": {"workspace_name": "Test space"},
                },
            )

        if (method, path) == ("POST", "/search"):
            assert payload["filter"] == {"property": "object", "value": "data_source"}
            return answer(200, self._paginate([self._data_source(i) for i in self.tables], payload))

        if path.startswith("/data_sources/"):
            parts = path.split("/")
            table_id = parts[2]
            if table_id not in self.tables:
                return error(404, "object_not_found", "Could not find data source.")
            if method == "GET" and len(parts) == 3:
                return answer(200, self._data_source(table_id))
            if method == "POST" and parts[3:] == ["query"]:
                pages = self.tables[table_id]["pages"]
                condition = (payload.get("filter") or {}).get("last_edited_time") or {}
                if "on_or_after" in condition:
                    since = _when(condition["on_or_after"])
                    pages = [p for p in pages if _when(p["last_edited_time"]) >= since]
                pages = sorted(pages, key=lambda p: _when(p["last_edited_time"]))
                return answer(200, self._paginate(pages, payload))

        return error(400, "invalid_request_url", f"Unknown endpoint {method} {path}")
