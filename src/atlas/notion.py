"""A small Notion API client: only what Atlas needs, standard library only.

Checked against the Notion reference on 2026-11-11 (API version 2026-03-11):

- ``GET /v1/users/me`` returns the bot user behind the token.
- ``POST /v1/search`` with ``{"filter": {"property": "object", "value":
  "data_source"}}`` lists the tables shared with the connection. Each result
  carries its full column schema.
- ``GET /v1/data_sources/{id}`` returns one table's schema.
- ``POST /v1/data_sources/{id}/query`` returns its rows, with cursor
  pagination and timestamp filters and sorts.
- Rate-limited responses (HTTP 429) carry a ``Retry-After`` header in seconds.
  Data source queries are limited to about 3 requests per second.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator

API_ROOT = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"

# Stay under the documented limit of about 3 requests per second.
MIN_INTERVAL = 0.35
MAX_ATTEMPTS = 5
RETRY_STATUSES = {429, 503, 529}

Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]


class NotionError(Exception):
    """An error answer from Notion, with a message meant for the user."""

    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.detail = status, code, message
        super().__init__(self._explain())

    def _explain(self) -> str:
        if self.status == 401:
            return "Notion rejected the token. Check that it was copied in full."
        if self.status == 403:
            return (
                "The Notion connection is not allowed to do this. "
                "It needs the 'read content' capability."
            )
        if self.status == 404:
            return (
                "Notion cannot find this table for the connection. "
                "Share the database with the connection, then try again."
            )
        if self.status == 429:
            return "Notion is rate limiting requests. Try again in a minute."
        return f"Notion answered with an error ({self.status} {self.code}): {self.detail}"


def _urllib_transport(method: str, url: str, headers: dict, body: bytes | None):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()
    except urllib.error.URLError as error:
        raise ConnectionError(f"Could not reach Notion: {error.reason}") from error


def plain_text(rich_text: list | None) -> str:
    """Text of a Notion rich text array, without formatting."""
    parts = []
    for item in rich_text or []:
        text = item.get("plain_text")
        if text is None:
            text = (item.get("text") or {}).get("content", "")
        parts.append(text)
    return "".join(parts)


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not token:
            raise ValueError("No Notion token. Enter one in Settings first.")
        self._token = token
        self._transport = transport or _urllib_transport
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None
        self.requests_made = 0

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")

        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._last_request is not None:
                wait = MIN_INTERVAL - (self._clock() - self._last_request)
                if wait > 0:
                    self._sleep(wait)
            self._last_request = self._clock()
            self.requests_made += 1
            status, response_headers, payload = self._transport(
                method, API_ROOT + path, headers, data
            )
            if status in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                # Every request Atlas makes here only reads, so a retry is safe.
                lowered = {key.lower(): value for key, value in response_headers.items()}
                try:
                    delay = float(lowered.get("retry-after", 1))
                except ValueError:
                    delay = 1.0
                self._sleep(min(max(delay, 0.0), 60.0))
                continue
            try:
                parsed = json.loads(payload.decode("utf-8")) if payload else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = {}
            if status >= 400:
                raise NotionError(
                    status, parsed.get("code", "unknown"), parsed.get("message", "no details")
                )
            return parsed
        raise AssertionError("unreachable")  # the loop always returns or raises

    def me(self) -> dict:
        """Who the token belongs to: connection name and workspace, when given."""
        user = self._request("GET", "/users/me")
        return {
            "name": user.get("name"),
            "workspace": (user.get("bot") or {}).get("workspace_name"),
        }

    def list_data_sources(self) -> list[dict]:
        """Every table shared with the connection, with its columns."""
        tables, cursor = [], None
        while True:
            body = {"filter": {"property": "object", "value": "data_source"}, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            page = self._request("POST", "/search", body)
            for item in page.get("results", []):
                if item.get("object") == "data_source" and not item.get("in_trash"):
                    tables.append(describe_data_source(item))
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                break
        return sorted(tables, key=lambda table: table["title"].lower())

    def retrieve_data_source(self, data_source_id: str) -> dict:
        return describe_data_source(self._request("GET", f"/data_sources/{data_source_id}"))

    def query_pages(
        self, data_source_id: str, *, edited_since: str | None = None
    ) -> Iterator[dict]:
        """Rows of a table, oldest edit first, optionally only recent edits.

        Raises NotionError if Notion reports that the result is incomplete, so
        a truncated listing is never mistaken for the whole table.
        """
        cursor = None
        while True:
            body: dict = {
                "page_size": 100,
                "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}],
            }
            if edited_since:
                body["filter"] = {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"on_or_after": edited_since},
                }
            if cursor:
                body["start_cursor"] = cursor
            page = self._request("POST", f"/data_sources/{data_source_id}/query", body)
            yield from (item for item in page.get("results", []) if item.get("object") == "page")
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                status = page.get("request_status") or {}
                if status.get("type") == "incomplete":
                    raise NotionError(
                        200,
                        "incomplete_result",
                        "Notion returned only part of the table "
                        f"({status.get('incomplete_reason', 'no reason given')}).",
                    )
                break


def describe_data_source(item: dict) -> dict:
    """The parts of a data source object Atlas uses."""
    parent = item.get("parent") or {}
    columns = [
        {"name": name, "type": (schema or {}).get("type", "unknown")}
        for name, schema in (item.get("properties") or {}).items()
    ]
    return {
        "id": item["id"],
        "title": plain_text(item.get("title")) or "Untitled",
        "database_id": parent.get("database_id"),
        "url": item.get("url"),
        "last_edited_time": item.get("last_edited_time"),
        "columns": sorted(columns, key=lambda column: column["name"].lower()),
    }
