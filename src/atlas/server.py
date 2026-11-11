"""The local interface: a small web server on this computer only.

``atlas ui`` starts it and opens the page in the browser. The page talks to a
JSON API under ``/api``; every call ends up in the same functions the command
line uses. The desktop shell planned for later wraps this same page.

Three guards keep other programs and web pages away from the API:

- the server listens on 127.0.0.1 only;
- requests must be addressed to localhost or 127.0.0.1 (protects against DNS
  rebinding);
- API calls must carry a random token that only the served page knows
  (protects against other sites making requests in the user's browser).
"""

from __future__ import annotations

import io
import json
import os
import secrets
import sys
import traceback
import webbrowser
from collections.abc import Callable
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from atlas import __version__, batch, bundle, db, groom, settings
from atlas.exporter import matching_ids, search_clause, status
from atlas.importer import import_csv_stream
from atlas.notion import NotionClient, NotionError
from atlas.notion_sync import guess_property_map, select_data_source, selected, sync

DEFAULT_PORT = 8765
MAX_BODY = 25_000_000  # room for a CSV export sent by the page
USER_ERRORS = (ValueError, NotionError, ConnectionError, FileNotFoundError, RuntimeError)


def app_version() -> str:
    """Version shown in the interface: the installed app's, else the package's."""
    return os.environ.get("ATLAS_APP_VERSION") or __version__


def _default_client() -> NotionClient:
    token = settings.get_token()
    if not token:
        raise ValueError("No Notion token yet. Enter one above.")
    return NotionClient(token)


class App:
    """What the request handlers share."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        client_factory: Callable[[], NotionClient] | None = None,
        token_checker: Callable[[str], dict] | None = None,
    ):
        self.db_path = Path(db_path)
        self.session_token = secrets.token_urlsafe(24)
        self.client_factory = client_factory or _default_client
        self.token_checker = token_checker or (lambda token: NotionClient(token).me())

    # Each call opens its own connection: requests run on separate threads.
    def _conn(self):
        return db.open_ready(self.db_path)

    def state(self) -> dict:
        conn = self._conn()
        try:
            return {
                "database": str(self.db_path),
                "version": app_version(),
                "status": status(conn),
                "notion": {
                    "token_set": settings.get_token() is not None,
                    "token_source": settings.token_source(),
                    "token_hint": settings.mask(settings.get_token()),
                    "selected": selected(conn),
                },
            }
        finally:
            conn.close()

    def set_token(self, body: dict) -> dict:
        token = str(body.get("token") or "").strip()
        if not token:
            raise ValueError("Paste the token first.")
        who = self.token_checker(token)  # nothing is stored if Notion refuses it
        where = settings.set_token(token)
        return {"connection": who, "stored_in": where}

    def forget_token(self) -> dict:
        settings.delete_token()
        return {"token_set": settings.get_token() is not None}

    def tables(self) -> dict:
        tables = self.client_factory().list_data_sources()
        for table in tables:
            table["guess"] = guess_property_map(table["columns"])
        return {"tables": tables}

    def select(self, body: dict) -> dict:
        table_id = str(body.get("data_source_id") or "")
        if not table_id:
            raise ValueError("Choose a table first.")
        mapping = body.get("property_map")
        if mapping is not None and not isinstance(mapping, dict):
            raise ValueError("property_map must be an object.")
        conn = self._conn()
        try:
            return {"selected": select_data_source(conn, self.client_factory(), table_id, mapping)}
        finally:
            conn.close()

    def sync(self, body: dict) -> dict:
        conn = self._conn()
        try:
            report = sync(
                conn,
                self.client_factory(),
                full=bool(body.get("full")),
                dry_run=bool(body.get("dry_run")),
            )
            return {"report": report.as_dict(), "status": status(conn)}
        finally:
            conn.close()

    def import_csv(self, body: dict) -> dict:
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Choose a file first.")
        content = content.lstrip("\ufeff")
        dry_run = bool(body.get("dry_run"))
        conn = self._conn()
        try:
            if content.lstrip().startswith("{"):
                # A bundle shared from another Atlas, not a CSV export.
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    raise ValueError("This file is neither a CSV export nor a bundle.") from None
                report = bundle.import_bundle(conn, data, dry_run=dry_run)
            else:
                report = import_csv_stream(
                    conn,
                    io.StringIO(content, newline=""),
                    dry_run=dry_run,
                    name=str(body.get("filename") or "the file"),
                )
            data = asdict(report)
            data["skipped"] = [{"id": str(key), "reason": why} for key, why in report.skipped]
            return {"report": data, "status": status(conn)}
        finally:
            conn.close()

    def bookmark_ids(self, query: dict) -> dict:
        conn = self._conn()
        try:
            return {"ids": matching_ids(conn, (query.get("q") or [""])[0])}
        finally:
            conn.close()

    def selection_tags(self, body: dict) -> dict:
        conn = self._conn()
        try:
            return {"tags": batch.tag_counts(conn, body.get("ids"))}
        finally:
            conn.close()

    def batch_edit(self, body: dict) -> dict:
        conn = self._conn()
        try:
            result = batch.apply_batch(
                conn,
                body.get("ids"),
                add_tags=body.get("add_tags") or [],
                remove_tags=body.get("remove_tags") or [],
                delete=bool(body.get("delete")),
                db_path=self.db_path,
            )
            return {"result": result, "status": status(conn)}
        finally:
            conn.close()

    def export_selection(self, body: dict) -> dict:
        ids = body.get("ids")
        if ids is not None and (not isinstance(ids, list) or not ids):
            raise ValueError("Select at least one bookmark.")
        conn = self._conn()
        try:
            return bundle.export_bundle(conn, None if ids is None else [str(i) for i in ids])
        finally:
            conn.close()

    def groom_scripts(self) -> dict:
        scripts, problems = groom.load_scripts(self.db_path)
        return {
            "scripts": [script.info() for script in scripts.values()],
            "problems": problems,
            "user_folder": str(groom.user_scripts_dir(self.db_path)),
        }

    def _script(self, body: dict):
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object.")
        return groom.get_script(str(body.get("script") or ""), self.db_path), params

    def groom_preview(self, body: dict) -> dict:
        script, params = self._script(body)
        conn = self._conn()
        try:
            return groom.preview(conn, script, params)
        finally:
            conn.close()

    def groom_apply(self, body: dict) -> dict:
        script, params = self._script(body)
        mark = body.get("fingerprint")
        if not isinstance(mark, str) or not mark:
            raise ValueError("Preview the script before applying it.")
        exclude = body.get("exclude") or []
        if not isinstance(exclude, list):
            raise ValueError("exclude must be a list.")
        conn = self._conn()
        try:
            result = groom.apply(
                conn,
                script,
                params,
                expected_fingerprint=mark,
                exclude={str(key) for key in exclude},
                db_path=self.db_path,
            )
            return {"result": result, "status": status(conn)}
        finally:
            conn.close()

    def groom_history(self) -> dict:
        conn = self._conn()
        try:
            return {"runs": groom.history(conn)}
        finally:
            conn.close()

    def groom_undo(self, body: dict) -> dict:
        conn = self._conn()
        try:
            result = groom.undo(conn, str(body.get("run_id") or ""), db_path=self.db_path)
            return {"result": result, "status": status(conn)}
        finally:
            conn.close()

    def bookmarks(self, query: dict) -> dict:
        text = (query.get("q") or [""])[0].strip()
        limit = max(1, min(int((query.get("limit") or ["100"])[0]), 500))
        offset = max(0, int((query.get("offset") or ["0"])[0]))
        where, params = search_clause(text)
        conn = self._conn()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM bookmarks WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT id, url, title, summary, domain, platform, saved_at, published_at "
                f"FROM bookmarks "
                f"WHERE {where} ORDER BY saved_at IS NULL, saved_at DESC, created_at DESC, id "
                "LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["tags"] = [
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM tags JOIN bookmark_tags ON tag_id = tags.id "
                        "WHERE bookmark_id = ? ORDER BY name COLLATE NOCASE",
                        (row["id"],),
                    )
                ]
                items.append(item)
            return {"total": total, "items": items}
        finally:
            conn.close()


def _page(app: App) -> bytes:
    html = resources.files("atlas.web").joinpath("index.html").read_text(encoding="utf-8")
    return html.replace("__ATLAS_SESSION_TOKEN__", app.session_token).encode("utf-8")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BookmarkAtlas"

        def log_message(self, format, *args):  # quiet: no request log on the terminal
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, data: dict) -> None:
            self._send(
                code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json"
            )

        def _host_ok(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("Request too large.")
            if not length:
                return {}
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            return data

        def _handle(self, method: str) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "This address is not allowed."})
            url = urlsplit(self.path)
            if method == "GET" and url.path in ("/", "/index.html"):
                return self._send(200, _page(app), "text/html; charset=utf-8")
            if not url.path.startswith("/api/"):
                return self._json(404, {"error": "Not found."})
            given = self.headers.get("X-Atlas-Token") or ""
            if not secrets.compare_digest(given, app.session_token):
                return self._json(403, {"error": "Reload the page and try again."})

            routes = {
                ("GET", "/api/state"): lambda: app.state(),
                ("GET", "/api/bookmarks"): lambda: app.bookmarks(parse_qs(url.query)),
                ("GET", "/api/notion/tables"): lambda: app.tables(),
                ("POST", "/api/notion/token"): lambda: app.set_token(self._body()),
                ("DELETE", "/api/notion/token"): lambda: app.forget_token(),
                ("POST", "/api/notion/select"): lambda: app.select(self._body()),
                ("POST", "/api/notion/sync"): lambda: app.sync(self._body()),
                ("POST", "/api/import-csv"): lambda: app.import_csv(self._body()),
                ("GET", "/api/bookmarks/ids"): lambda: app.bookmark_ids(parse_qs(url.query)),
                ("POST", "/api/bookmarks/tags"): lambda: app.selection_tags(self._body()),
                ("POST", "/api/bookmarks/batch"): lambda: app.batch_edit(self._body()),
                ("POST", "/api/bookmarks/export"): lambda: app.export_selection(self._body()),
                ("GET", "/api/groom/scripts"): lambda: app.groom_scripts(),
                ("GET", "/api/groom/history"): lambda: app.groom_history(),
                ("POST", "/api/groom/preview"): lambda: app.groom_preview(self._body()),
                ("POST", "/api/groom/apply"): lambda: app.groom_apply(self._body()),
                ("POST", "/api/groom/undo"): lambda: app.groom_undo(self._body()),
            }
            route = routes.get((method, url.path))
            if route is None:
                return self._json(404, {"error": "Not found."})
            try:
                self._json(200, route())
            except USER_ERRORS as error:
                self._json(400, {"error": str(error)})
            except json.JSONDecodeError:
                self._json(400, {"error": "The request was not valid JSON."})
            except Exception:
                traceback.print_exc(file=sys.stderr)
                self._json(500, {"error": "Something went wrong. Details are in the terminal."})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return Handler


def make_server(app: App, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Bind to this computer only. Port 0 picks a free one."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    return server


def serve(
    db_path: Path | str,
    *,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    announce: bool = False,
) -> None:
    """Run the interface until interrupted.

    With ``announce``, the address is printed as one machine-readable line
    (``ATLAS_READY {"url": ...}``) for the desktop shell that started us.
    """
    app = App(db_path)
    try:
        server = make_server(app, port)
    except OSError:
        if port != DEFAULT_PORT:
            raise
        server = make_server(app, 0)  # the usual port is taken: use any free one
    address = f"http://127.0.0.1:{server.server_address[1]}/"
    if announce:
        print("ATLAS_READY " + json.dumps({"url": address}), flush=True)
    else:
        print(f"Bookmark Atlas is running at {address}", flush=True)
        print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if not announce:
            print("\nStopped.")
    finally:
        server.server_close()
