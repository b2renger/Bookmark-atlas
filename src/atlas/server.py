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

from atlas import (
    __version__,
    batch,
    bundle,
    db,
    groom,
    settings,
    snapshot,
    sqldump,
    views,
    workspaces,
)
from atlas.db import now_iso
from atlas.exporter import facet_options, matching_ids, search_clause, status
from atlas.importer import (
    import_browser_bookmarks,
    import_csv_stream,
    looks_like_browser_export,
)
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
        self.home_path = Path(db_path)  # the main collection; db_path moves to a workspace
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
                "data_folder": str(self.db_path.resolve().parent),
                "snapshot": snapshot.snapshot_info(conn),
                "workspace": workspaces.info(conn) if self.db_path != self.home_path else None,
                "progress": _progress(conn),
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

    def tutorial(self, body: dict) -> dict:
        """Remember that the getting-started steps were put away, or bring them back."""
        conn = self._conn()
        try:
            key = "tutorial_model_hidden" if body.get("tour") == "model" else "tutorial_hidden"
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, "1" if body.get("hidden") else "0"),
            )
            conn.commit()
            return {"progress": _progress(conn)}
        finally:
            conn.close()

    # ---- workspaces ----

    def workspaces_list(self) -> dict:
        home = db.open_ready(self.home_path)
        try:
            count = home.execute(
                "SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL"
            ).fetchone()[0]
        finally:
            home.close()
        active = None if self.db_path == self.home_path else self.db_path.parent.name
        return {
            "home": {"bookmarks": count},
            "active": active,
            "workspaces": workspaces.list_all(self.home_path),
            "folder": str(workspaces.root(self.home_path)),
        }

    def workspace_create(self, body: dict) -> dict:
        start = str(body.get("start") or "empty")
        ids = body.get("ids")
        if start == "ids" and (not isinstance(ids, list) or not ids):
            raise ValueError("There is no bookmark to start the workspace from.")
        conn = self._conn()
        try:
            made = workspaces.create(
                conn,
                self.home_path,
                str(body.get("name") or ""),
                ids=[str(i) for i in ids] if start == "ids" else None,
                empty=start == "empty",
                started_from=str(body.get("started_from") or ""),
            )
        finally:
            conn.close()
        self.db_path = workspaces.path_of(self.home_path, made["id"])
        return {"created": made, **self.workspaces_list()}

    def workspace_open(self, body: dict) -> dict:
        wanted = str(body.get("id") or "")
        self.db_path = (
            self.home_path if wanted in ("", "home") else workspaces.path_of(self.home_path, wanted)
        )
        return self.workspaces_list()

    def workspace_delete(self, body: dict) -> dict:
        wanted = str(body.get("id") or "")
        target = workspaces.path_of(self.home_path, wanted)
        if target == self.db_path:
            self.db_path = self.home_path
        workspaces.delete(self.home_path, wanted)
        return self.workspaces_list()

    def workspace_merge(self, body: dict) -> dict:
        home = db.open_ready(self.home_path)
        try:
            result = workspaces.merge(
                home,
                self.home_path,
                str(body.get("id") or ""),
                add_tags=body.get("add_tags") is not False,
            )
        finally:
            home.close()
        return {"merged": result, **self.workspaces_list()}

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

    def browsers(self, body: dict | None = None) -> dict:
        """The browsers found on this computer; with ``id``, preview or import one."""
        from atlas import browsers
        from atlas.importer import import_browser_items

        if not body or not body.get("id"):
            listed = []
            for entry in browsers.found():
                try:
                    counts = browsers.summary(entry["id"])
                    listed.append({**entry, **counts, "problem": None})
                except ValueError as error:
                    listed.append({**entry, "bookmarks": None, "other": 0, "problem": str(error)})
            return {"browsers": listed}
        items = browsers.read(str(body["id"]))
        dry_run = bool(body.get("dry_run"))
        conn = self._conn()
        try:
            report = import_browser_items(conn, items, dry_run=dry_run)
            data = asdict(report)
            data["skipped"] = [{"id": str(key), "reason": why} for key, why in report.skipped]
            return {"report": data, "status": status(conn)}
        finally:
            conn.close()

    def _import_sql(self, content: str, filename: str, dry_run: bool) -> dict:
        """A database dump goes into a workspace of its own, which is then opened."""
        name = Path(filename).stem.replace("_", " ").strip() or "Imported"
        if dry_run:
            figures = sqldump.describe(sqldump.read_dump(content))
            created = None
        else:
            made = sqldump.into_workspace(self.home_path, content, name)
            figures, created = made["reading"], made["workspace"]
            self.db_path = workspaces.path_of(self.home_path, created["id"])
        conn = self._conn()
        try:
            return {"dump": figures, "workspace": created, "name": name, "status": status(conn)}
        finally:
            conn.close()

    def import_csv(self, body: dict) -> dict:
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Choose a file first.")
        content = content.lstrip("\ufeff")
        dry_run = bool(body.get("dry_run"))
        if sqldump.looks_like_sql_dump(content):
            return self._import_sql(content, str(body.get("filename") or "Imported"), dry_run)
        conn = self._conn()
        try:
            if looks_like_browser_export(content):
                report = import_browser_bookmarks(conn, content, dry_run=dry_run)
            elif content.lstrip().startswith("{"):
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
            return {
                "ids": matching_ids(
                    conn,
                    (query.get("q") or [""])[0],
                    (query.get("cluster") or [None])[0],
                    _facets(query),
                )
            }
        finally:
            conn.close()

    # ---- language models ----

    def _with(self, work, *, model: bool = False, route: str | None = None):
        """Run ``work(conn[, connection])`` on the open collection. ``route``
        names a kept connection to use in place of the one in use."""
        from atlas import llm

        connection = llm.Connection(slot=route or None) if model else None
        conn = self._conn()
        try:
            return work(conn, connection) if model else work(conn)
        finally:
            conn.close()

    def llm_state(self) -> dict:
        from atlas import assist, llm

        return {
            "connection": llm.describe(),
            "vectors": self._with(assist.vector_status),
            "providers": llm.providers(),
            "kept": llm.kept(),
            "keys_in": settings.kept_in(),
        }

    def llm_find(self, body: dict) -> dict:
        from atlas import llm

        if body.get("host"):
            return {"farms": [llm.read_self(str(body["host"]))]}
        return {"farms": llm.discover(seconds=min(float(body.get("seconds") or 6), 15))}

    def _llm_config(self, body: dict) -> dict:
        config = {
            key: str(body.get(key) or "").strip()
            for key in (
                "kind",
                "name",
                "base_url",
                "self_url",
                "chat_model",
                "embed_url",
                "embed_model",
            )
        }
        from atlas import llm

        if config["kind"] == "local":
            from atlas import engine

            if config["chat_model"] not in engine.WRITERS:
                raise ValueError("Choose one of the three sizes.")
            return {**dict.fromkeys(config, ""), "kind": "local", "name": "This computer",
                    "base_url": "local", "chat_model": config["chat_model"]}  # fmt: skip
        provider = llm.PROVIDERS.get(config["kind"])
        if provider:
            # A provider's address is its own: a key is never sent to one typed in.
            config["base_url"] = provider["base_url"]
            config["name"] = provider["name"]
            config["self_url"] = config["embed_url"] = ""
            return config
        if not config["base_url"].startswith(("http://", "https://")):
            raise ValueError("The address must start with http:// or https://")
        config["base_url"] = config["base_url"].rstrip("/")
        if not config["base_url"].endswith("/v1"):
            config["base_url"] += "/v1"
        return config

    def llm_test(self, body: dict) -> dict:
        from atlas import llm

        password = body.get("password")
        if password is None and body.get("keep_password"):
            password = llm.kept_key_for(self._llm_config(body))
        return llm.test(self._llm_config(body), password)

    def llm_connect(self, body: dict) -> dict:
        from atlas import llm

        config = self._llm_config(body)
        if not config["chat_model"]:
            raise ValueError("Choose a model.")
        slot = llm.slot_of(config)
        kept = llm.kept_key_for(config) if body.get("keep_password") else None
        llm.save(config)
        if body.get("password") or kept:
            settings.set_secret(llm.secret_name(slot), str(body.get("password") or kept))
        else:
            settings.delete_secret(llm.secret_name(slot))
        return self.llm_state()

    def llm_use(self, body: dict) -> dict:
        """Switch to a connection kept earlier, or forget one."""
        from atlas import engine, llm

        slot = str(body.get("slot") or "")
        if body.get("forget"):
            if slot == "local":
                engine.stop()
            llm.forget(slot)
        else:
            llm.activate(slot)
        return self.llm_state()

    def engine(self, body: dict | None = None) -> dict:
        """The model inside the app: what is there, downloads, and which one is used."""
        from atlas import engine, llm

        body = body or {}
        action, model = str(body.get("action") or ""), str(body.get("model") or "")
        if action == "download":
            engine.download(model)
        elif action == "cancel":
            engine.cancel(model)
        elif action == "remove":
            engine.remove(model)
            if llm.load().get("kind") == "local" and llm.load().get("chat_model") == model:
                llm.forget()
        elif action == "stop":
            engine.stop()
        elif action:
            raise ValueError("Unknown action.")
        return engine.state()

    def llm_forget(self) -> dict:
        from atlas import engine, llm

        engine.stop()
        llm.forget()
        return self.llm_state()

    def llm_vectors(self, body: dict) -> dict:
        from atlas import assist

        if body.get("source"):
            return self._with(lambda conn: assist.set_vector_source(conn, str(body["source"])))
        return self._with(
            lambda conn, connection: assist.compute_vectors(
                conn, connection, limit=int(body.get("limit") or 96)
            ),
            model=True,
        )

    @staticmethod
    def _ids(body: dict) -> list[str] | None:
        ids = body.get("ids")
        return None if ids is None else [str(item) for item in ids][:50_000]

    def pages(self, body: dict | None = None) -> dict:
        from atlas import assist

        if body is None or body.get("status_only"):
            ids = self._ids(body or {})
            return self._with(lambda conn: assist.pages_status(conn, ids))
        return self._with(
            lambda conn: assist.read_pages(
                conn,
                ids=self._ids(body),
                limit=int(body.get("limit") or 12),
                retry_failed=bool(body.get("retry_failed")),
                again=bool(body.get("again")),
            )
        )

    def notes(self, body: dict | None = None) -> dict:
        from atlas import assist

        if body is None or body.get("status_only"):
            ids = self._ids(body or {})
            return self._with(lambda conn: {**assist.notes_status(conn, ids), "now": now_iso()})
        return self._with(
            lambda conn, connection: assist.write_notes(
                conn,
                connection,
                ids=self._ids(body),
                limit=int(body.get("limit") or 6),
                language=str(body.get("language") or "page"),
                since=str(body["since"]) if body.get("since") else None,
            ),
            model=True,
        )

    def notebooks(self, body: dict | None = None) -> dict:
        """Everything a notebook does, by ``action``; without one, the list."""
        from atlas import notebook

        body = body or {}
        action = str(body.get("action") or "list")
        book = str(body.get("id") or "")
        if action in ("ask", "overview"):
            work = (
                (lambda c, m: notebook.ask(c, m, book, str(body.get("question") or "")))
                if action == "ask"
                else (lambda c, m: notebook.overview(c, m, book, str(body.get("kind") or "")))
            )
            return {"message": self._with(work, model=True, route=str(body.get("route") or ""))}
        actions = {
            "list": lambda c: {"notebooks": notebook.list_all(c)},
            "create": lambda c: notebook.create(
                c, str(body.get("name") or ""), self._ids(body) or []
            ),
            "show": lambda c: notebook.show(c, book),
            "rename": lambda c: notebook.rename(c, book, str(body.get("name") or "")),
            "delete": lambda c: notebook.delete(c, book) or {"notebooks": notebook.list_all(c)},
            "sources": lambda c: notebook.set_sources(
                c,
                book,
                add=[str(i) for i in body.get("add") or []],
                remove=[str(i) for i in body.get("remove") or []],
                use={str(k): bool(v) for k, v in (body.get("use") or {}).items()},
            ),
            "clear": lambda c: notebook.clear(c, book),
            "keep": lambda c: notebook.keep(
                c,
                book,
                message_id=body.get("message_id"),
                title=str(body.get("title") or ""),
                text=str(body.get("text") or ""),
            ),
            "drop_note": lambda c: notebook.drop_note(c, book, str(body.get("note_id") or "")),
            "markdown": lambda c: {"markdown": notebook.as_markdown(c, book)},
        }
        if action not in actions:
            raise ValueError("Unknown notebook action.")
        return self._with(actions[action])

    def tag_groups(self, body: dict) -> dict:
        from atlas import assist

        if body.get("status_only"):
            return self._with(assist.tag_groups_status)
        return self._with(
            lambda conn, connection: assist.find_tag_groups(
                conn, connection, start=bool(body.get("start"))
            ),
            model=True,
        )

    def compare(self, body: dict) -> dict:
        from atlas import assist

        if body.get("status_only"):
            ids = self._ids(body)
            return self._with(lambda conn: {**assist.compare_status(conn, ids), "now": now_iso()})
        return self._with(
            lambda conn, connection: assist.compare_pages(
                conn,
                connection,
                ids=self._ids(body),
                limit=int(body.get("limit") or 6),
                since=str(body["since"]) if body.get("since") else None,
            ),
            model=True,
        )

    def ask(self, body: dict) -> dict:
        from atlas import assist

        if body.get("collection"):
            return self._with(
                lambda conn, connection: assist.ask_collection(
                    conn, connection, str(body.get("question") or "")
                ),
                model=True,
            )
        return self._with(
            lambda conn, connection: assist.ask(
                conn,
                connection,
                self._ids(body) or [],
                str(body.get("kind") or "question"),
                str(body.get("question") or ""),
            ),
            model=True,
        )

    def answers(self, body: dict | None = None) -> dict:
        from atlas import assist

        if body and body.get("delete"):
            self._with(lambda conn: assist.forget_answer(conn, str(body["delete"])))
        return {"answers": self._with(assist.answers)}

    def llm_explain(self, body: dict) -> dict:
        from atlas import assist

        return self._with(
            lambda conn, connection: assist.explain_link(
                conn, connection, str(body.get("a")), str(body.get("b"))
            ),
            model=True,
        )

    def llm_topic_names(self, body: dict) -> dict:
        from atlas import assist

        return self._with(
            lambda conn, connection: assist.propose_topic_names(
                conn, connection, str(body.get("run_id"))
            ),
            model=True,
        )

    def facets(self) -> dict:
        conn = self._conn()
        try:
            return facet_options(conn)
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

    def grid_show(self) -> dict:
        from atlas.analysis import store

        conn = self._conn()
        try:
            return store.grid_data(conn)
        finally:
            conn.close()

    def grid_new(self) -> dict:
        from atlas.analysis import store

        conn = self._conn()
        try:
            return store.grid_run(conn)
        finally:
            conn.close()

    def tree_show(self) -> dict:
        from atlas.analysis import store

        conn = self._conn()
        try:
            return store.tree_data(conn)
        finally:
            conn.close()

    def tree_new(self, body: dict) -> dict:
        from atlas.analysis import store

        conn = self._conn()
        try:
            return store.tree_run(conn, body.get("cluster_run") or None)
        finally:
            conn.close()

    def tree_edit(self, body: dict) -> dict:
        from atlas.analysis import store

        conn = self._conn()
        try:
            return store.edit_tree(
                conn,
                str(body.get("action") or ""),
                node=body.get("node"),
                name=body.get("name"),
                family=body.get("family"),
                other=body.get("other"),
            )
        finally:
            conn.close()

    def links_show(self, query: dict | None = None) -> dict:
        from atlas.analysis import links

        conn = self._conn()
        try:
            only = _passing(conn, query or {})
            return {
                **links.overview(conn, only=only),
                "resurface": links.resurface(conn, only=only),
                "filtered": only is not None,
            }
        finally:
            conn.close()

    def links_related(self, query: dict) -> dict:
        from atlas.analysis import links

        conn = self._conn()
        try:
            return links.related(conn, (query.get("id") or [""])[0])
        finally:
            conn.close()

    def links_threads(self) -> dict:
        from atlas.analysis import links

        conn = self._conn()
        try:
            return links.threads(conn)
        finally:
            conn.close()

    def trends_show(self, query: dict | None = None) -> dict:
        from atlas import trends

        conn = self._conn()
        try:
            only = _passing(conn, query or {})
            filtered = only is not None
            run_id, names = None, None
            try:
                from atlas.analysis import store

                run_id = store.latest_run_id(conn)
                if run_id:
                    described = store.describe_run(conn, run_id, samples=0)
                    names = {item["cluster"]: item["label"] for item in described["clusters"]}
            except RuntimeError:
                pass  # no NumPy: dates and tags still work, topics are left out
            return {**trends.overview(conn, run_id, names, only), "filtered": filtered}
        finally:
            conn.close()

    def views_list(self) -> dict:
        conn = self._conn()
        try:
            return {"views": views.list_views(conn)}
        finally:
            conn.close()

    def view_get(self, query: dict) -> dict:
        conn = self._conn()
        try:
            return {"view": views.get_view(conn, (query.get("id") or [""])[0])}
        finally:
            conn.close()

    def view_save(self, body: dict) -> dict:
        conn = self._conn()
        try:
            view = views.save_view(conn, body.get("name"), body.get("state"), body.get("note"))
            return {"view": view, "views": views.list_views(conn)}
        finally:
            conn.close()

    def view_delete(self, body: dict) -> dict:
        conn = self._conn()
        try:
            views.delete_view(conn, str(body.get("id") or ""))
            return {"views": views.list_views(conn)}
        finally:
            conn.close()

    def snapshot_export(self, body: dict) -> bytes:
        """Build a snapshot and return its bytes; nothing is left on disk."""
        import tempfile

        ids = body.get("ids")
        if ids is not None and (not isinstance(ids, list) or not ids):
            raise ValueError("Select at least one bookmark, or export everything.")
        conn = self._conn()
        try:
            with tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / ("export" + snapshot.SUFFIX)
                snapshot.export_snapshot(
                    conn, target, ids=ids, name=body.get("name"), note=body.get("note")
                )
                return target.read_bytes()
        finally:
            conn.close()

    def cluster_runs(self) -> dict:
        from atlas.analysis import store

        conn = self._conn()
        try:
            return {"runs": store.list_runs(conn), "default_algorithm": store.DEFAULT_ALGORITHM}
        finally:
            conn.close()

    def cluster_show(self, query: dict) -> dict:
        from atlas.analysis import store

        conn = self._conn()
        try:
            run_id = (query.get("id") or [""])[0] or store.latest_run_id(conn)
            return {"run": store.describe_run(conn, run_id) if run_id else None}
        finally:
            conn.close()

    def cluster_new(self, body: dict) -> dict:
        from atlas.analysis import store

        k = body.get("k")
        if k in (None, "", "auto"):
            k = None
        else:
            try:
                k = int(k)
            except (TypeError, ValueError):
                raise ValueError("The number of clusters must be a whole number.") from None
        conn = self._conn()
        try:
            run = store.cluster_run(
                conn,
                k,
                algorithm=str(body.get("algorithm") or store.DEFAULT_ALGORITHM),
                seed=int(body.get("seed") or 0),
            )
            return {"run": run}
        finally:
            conn.close()

    def cluster_label(self, body: dict) -> dict:
        from atlas.analysis import store

        conn = self._conn()
        try:
            run_id = str(body.get("run_id") or "")
            store.set_cluster_label(conn, run_id, int(body.get("cluster")), body.get("label"))
            return {"run": store.describe_run(conn, run_id)}
        finally:
            conn.close()

    def cluster_delete(self, body: dict) -> dict:
        from atlas.analysis import store

        conn = self._conn()
        try:
            store.delete_run(conn, str(body.get("run_id") or ""))
            return {"runs": store.list_runs(conn)}
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

    def dates_status(self) -> dict:
        from atlas import pagedates

        conn = self._conn()
        try:
            return pagedates.status(conn)
        finally:
            conn.close()

    def dates_lookup(self, body: dict) -> dict:
        """Look one batch of pages up. The page calls this again until none is left."""
        from atlas import pagedates

        conn = self._conn()
        try:
            return pagedates.lookup(
                conn,
                limit=int(body.get("limit") or 12),
                retry_failed=bool(body.get("retry_failed")),
                skip_github=bool(body.get("skip_github")),
            )
        finally:
            conn.close()

    def links_check_status(self) -> dict:
        from atlas import pagedates

        conn = self._conn()
        try:
            return pagedates.link_status(conn)
        finally:
            conn.close()

    def links_check(self, body: dict) -> dict:
        from atlas import pagedates

        conn = self._conn()
        try:
            return pagedates.check_links(
                conn, limit=int(body.get("limit") or 12), again=bool(body.get("again"))
            )
        finally:
            conn.close()

    def snapshot_compare(self, body: dict) -> dict:
        """Compare this collection with a snapshot file sent by the page, and
        on request bring in the bookmarks it has that this collection lacks."""
        import base64
        import binascii
        import tempfile

        from atlas import snapshot

        try:
            content = base64.b64decode(str(body.get("content_b64") or ""), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("The file could not be read.") from None
        if not content:
            raise ValueError("Choose a snapshot file first.")
        with tempfile.TemporaryDirectory(prefix="atlas-compare-") as folder:
            target = Path(folder) / "other.sqlite"
            target.write_bytes(content)
            conn = self._conn()
            try:
                result = {}
                if body.get("adopt"):
                    report = snapshot.adopt(conn, target)
                    conn.commit()
                    result["adopted"] = report.created
                result.update(snapshot.compare(conn, target))
                result["status"] = status(conn)
                return result
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

    def map_show(self, query: dict) -> dict:
        from atlas.analysis import store

        conn = self._conn()
        try:
            return store.map_data(conn, (query.get("clusters") or [None])[0] or None)
        finally:
            conn.close()

    def map_new(self, body: dict) -> dict:
        from atlas.analysis import store

        conn = self._conn()
        try:
            if store.latest_run_id(conn) is None:
                store.cluster_run(conn)  # a map without topics says little: make them first
            store.project_run(conn, seed=int(body.get("seed") or 0))
            return store.map_data(conn)
        finally:
            conn.close()

    def bookmark_item(self, query: dict) -> dict:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id, url, title, summary, domain, platform, saved_at, published_at "
                "FROM bookmarks WHERE id = ? AND deleted_at IS NULL",
                ((query.get("id") or [""])[0],),
            ).fetchone()
            if row is None:
                raise ValueError("No such bookmark.")
            item = dict(row)
            item["tags"] = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM tags JOIN bookmark_tags ON tag_id = tags.id "
                    "WHERE bookmark_id = ? ORDER BY name COLLATE NOCASE",
                    (row["id"],),
                )
            ]
            from atlas import assist

            item["model"] = assist.about(conn, row["id"])
            return {"item": item}
        finally:
            conn.close()

    def bookmarks_listed(self, body: dict) -> dict:
        """The bookmark list restricted to given IDs (the current selection)."""
        ids = body.get("ids")
        if not isinstance(ids, list):
            raise ValueError("ids must be a list.")
        query = {
            "q": [str(body.get("q") or "")],
            "limit": [str(body.get("limit") or 100)],
            "offset": [str(body.get("offset") or 0)],
        }
        chosen = body.get("facets") if isinstance(body.get("facets"), dict) else {}
        tags = chosen.get("tags") if isinstance(chosen.get("tags"), list) else []
        query["tag"] = [str(tag) for tag in tags]
        for key, name in (("platform", "platform"), ("date_from", "from"), ("date_to", "to")):
            query[name] = [str(chosen.get(key) or "")]
        query["dates"] = [str(chosen.get("date_kind") or "when")]
        if body.get("ids_only"):  # "select all" inside "only these": the same narrowing
            passing = set(self.bookmark_ids(query)["ids"])
            return {"ids": [str(i) for i in ids if str(i) in passing]}
        return self.bookmarks(query, only=[str(i) for i in ids])

    def bookmarks(self, query: dict, only: list[str] | None = None) -> dict:
        text = (query.get("q") or [""])[0].strip()
        limit = max(1, min(int((query.get("limit") or ["100"])[0]), 500))
        offset = max(0, int((query.get("offset") or ["0"])[0]))
        where, params = search_clause(text, (query.get("cluster") or [None])[0], _facets(query))
        conn = self._conn()
        try:
            if only is not None:
                conn.execute("CREATE TEMP TABLE wanted (id TEXT PRIMARY KEY)")
                conn.executemany("INSERT OR IGNORE INTO wanted VALUES (?)", [(i,) for i in only])
                where += " AND id IN (SELECT id FROM wanted)"
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


def _progress(conn) -> dict:
    """How far this collection has come, for the getting-started steps."""
    kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM runs")}
    count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    hidden = conn.execute("SELECT value FROM meta WHERE key = 'tutorial_hidden'").fetchone()
    return {
        "bookmarks": count("SELECT COUNT(*) FROM bookmarks WHERE deleted_at IS NULL") > 0,
        "groomed": count("SELECT COUNT(*) FROM script_runs") > 0,
        "clustered": "cluster" in kinds,
        "mapped": "projection" in kinds,
        "tree": "tree" in kinds,
        "saved_view": count("SELECT COUNT(*) FROM views") > 0,
        "hidden": bool(hidden and hidden[0] == "1"),
        "notebooks": conn.execute("SELECT COUNT(*) FROM notebooks").fetchone()[0],
        "model": _model_progress(conn),
    }


def _model_progress(conn) -> dict:
    """How far the language-model steps have come, for their own short tour."""
    from atlas import assist, llm

    count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    put_away = conn.execute("SELECT value FROM meta WHERE key = 'tutorial_model_hidden'").fetchone()
    return {
        "connected": bool(llm.load().get("base_url")),
        "meaning": assist.vector_status(conn)["in_use"] == "model",
        "notes": count("SELECT COUNT(*) FROM llm_notes WHERE kind = 'summary'") > 0,
        "asked": count("SELECT COUNT(*) FROM llm_answers") > 0
        or count("SELECT COUNT(*) FROM notebook_messages") > 0,
        "hidden": bool(put_away and put_away[0] == "1"),
    }


def _passing(conn, query: dict) -> set[str] | None:
    """The bookmarks that pass the shared filter, or None when nothing is filtered."""
    chosen = _facets(query)
    active = (
        [t for t in chosen["tags"] if t.strip()]
        or chosen["platform"]
        or chosen["date_from"]
        or chosen["date_to"]
    )
    return set(matching_ids(conn, "", None, chosen)) if active else None


def _facets(query: dict) -> dict:
    """The shared filter, as it arrives in a query string."""
    first = lambda key: (query.get(key) or [""])[0]  # noqa: E731
    return {
        "tags": query.get("tag") or [],
        "platform": first("platform"),
        "date_from": first("from"),
        "date_to": first("to"),
        "date_kind": first("dates") or "when",
    }


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
            try:
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the page went away before the answer: nothing to report

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
                ("GET", "/api/facets"): lambda: app.facets(),
                ("GET", "/api/links/check"): lambda: app.links_check_status(),
                ("POST", "/api/links/check"): lambda: app.links_check(self._body()),
                ("GET", "/api/dates/lookup"): lambda: app.dates_status(),
                ("POST", "/api/dates/lookup"): lambda: app.dates_lookup(self._body()),
                ("POST", "/api/bookmarks/tags"): lambda: app.selection_tags(self._body()),
                ("POST", "/api/bookmarks/batch"): lambda: app.batch_edit(self._body()),
                ("POST", "/api/bookmarks/export"): lambda: app.export_selection(self._body()),
                ("GET", "/api/map"): lambda: app.map_show(parse_qs(url.query)),
                ("POST", "/api/map"): lambda: app.map_new(self._body()),
                ("GET", "/api/bookmarks/item"): lambda: app.bookmark_item(parse_qs(url.query)),
                ("POST", "/api/bookmarks/list"): lambda: app.bookmarks_listed(self._body()),
                ("GET", "/api/grid"): lambda: app.grid_show(),
                ("POST", "/api/grid"): lambda: app.grid_new(),
                ("GET", "/api/tree"): lambda: app.tree_show(),
                ("POST", "/api/tree"): lambda: app.tree_new(self._body()),
                ("POST", "/api/tree/edit"): lambda: app.tree_edit(self._body()),
                ("GET", "/api/links"): lambda: app.links_show(parse_qs(url.query)),
                ("GET", "/api/links/related"): lambda: app.links_related(parse_qs(url.query)),
                ("GET", "/api/links/threads"): lambda: app.links_threads(),
                ("GET", "/api/trends"): lambda: app.trends_show(parse_qs(url.query)),
                ("POST", "/api/tutorial"): lambda: app.tutorial(self._body()),
                ("GET", "/api/workspaces"): lambda: app.workspaces_list(),
                ("POST", "/api/workspaces"): lambda: app.workspace_create(self._body()),
                ("POST", "/api/workspaces/open"): lambda: app.workspace_open(self._body()),
                ("POST", "/api/workspaces/delete"): lambda: app.workspace_delete(self._body()),
                ("POST", "/api/workspaces/merge"): lambda: app.workspace_merge(self._body()),
                ("GET", "/api/browsers"): lambda: app.browsers(),
                ("POST", "/api/browsers"): lambda: app.browsers(self._body()),
                ("GET", "/api/llm"): lambda: app.llm_state(),
                ("GET", "/api/engine"): lambda: app.engine(),
                ("POST", "/api/engine"): lambda: app.engine(self._body()),
                ("POST", "/api/llm/find"): lambda: app.llm_find(self._body()),
                ("POST", "/api/llm/test"): lambda: app.llm_test(self._body()),
                ("POST", "/api/llm/connect"): lambda: app.llm_connect(self._body()),
                ("POST", "/api/llm/forget"): lambda: app.llm_forget(),
                ("POST", "/api/llm/use"): lambda: app.llm_use(self._body()),
                ("POST", "/api/llm/vectors"): lambda: app.llm_vectors(self._body()),
                ("POST", "/api/llm/explain"): lambda: app.llm_explain(self._body()),
                ("POST", "/api/llm/topic-names"): lambda: app.llm_topic_names(self._body()),
                ("POST", "/api/pages"): lambda: app.pages(self._body()),
                ("POST", "/api/notes"): lambda: app.notes(self._body()),
                ("POST", "/api/ask"): lambda: app.ask(self._body()),
                ("GET", "/api/notebooks"): lambda: app.notebooks(),
                ("POST", "/api/notebooks"): lambda: app.notebooks(self._body()),
                ("POST", "/api/tags/groups"): lambda: app.tag_groups(self._body()),
                ("POST", "/api/compare"): lambda: app.compare(self._body()),
                ("GET", "/api/answers"): lambda: app.answers(),
                ("POST", "/api/answers"): lambda: app.answers(self._body()),
                ("GET", "/api/views"): lambda: app.views_list(),
                ("GET", "/api/views/item"): lambda: app.view_get(parse_qs(url.query)),
                ("POST", "/api/views"): lambda: app.view_save(self._body()),
                ("POST", "/api/views/delete"): lambda: app.view_delete(self._body()),
                ("POST", "/api/snapshot"): lambda: app.snapshot_export(self._body()),
                ("POST", "/api/snapshot/compare"): lambda: app.snapshot_compare(self._body()),
                ("GET", "/api/clusters/runs"): lambda: app.cluster_runs(),
                ("GET", "/api/clusters/run"): lambda: app.cluster_show(parse_qs(url.query)),
                ("POST", "/api/clusters/run"): lambda: app.cluster_new(self._body()),
                ("POST", "/api/clusters/label"): lambda: app.cluster_label(self._body()),
                ("POST", "/api/clusters/delete"): lambda: app.cluster_delete(self._body()),
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
                answer = route()
                if isinstance(answer, bytes):
                    self._send(200, answer, "application/octet-stream")
                else:
                    self._json(200, answer)
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
