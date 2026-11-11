"""Grooming: cleaning the collection with scripts that can be previewed and undone.

A script only *proposes* changes: it reads the database and returns a list of
``Change`` objects. This engine does everything that writes:

- **preview** runs the script and returns its changes, grouped for display,
  with a fingerprint of the whole proposal;
- **apply** runs the script again, checks the proposal is still the one that
  was previewed, backs the database up, writes the accepted groups in one
  transaction and records them in the journal;
- **undo** reverses a run from the journal.

Every write checks that the value being replaced is still the one the script
saw. If anything moved in between, the whole run is refused and nothing is
written.

See docs/GROOMING.md for how to write a script.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType

from atlas.db import now_iso

BUILTIN = [
    "merge_duplicates",
    "normalize_tags",
    "derive_platform",
    "published_from_x_id",
    "split_dates",
]

# Bookmark columns a script may change. Anything else is refused.
BOOKMARK_FIELDS = {
    "title",
    "summary",
    "platform",
    "saved_at",
    "saved_source",
    "published_at",
    "published_source",
    "url_normalized",
    "domain",
    "deleted_at",
    "merged_into",
}

KEEP_BACKUPS = 10

# A script cannot know the time at which its changes will be applied, and a
# clock value would make every preview differ from the next. It writes NOW
# instead; the engine replaces it at the moment of writing.
NOW = "@now"


class GroomError(ValueError):
    """A grooming request that cannot be carried out. The message is for the user."""


@dataclass(frozen=True)
class Change:
    """One proposed edit.

    ``entity`` and ``field`` say what is touched:

    =============  ============  ===========================================
    entity         field         old -> new
    =============  ============  ===========================================
    bookmark       a column      previous value -> new value
    bookmark_tag   ``tag``       ``None`` -> name (add) or name -> ``None``
    tag            ``alias_of``  ``None`` -> name of the tag it merges into
    source_record  ``pending``   ``"1"`` -> ``"0"``
    =============  ============  ===========================================

    ``entity_id`` is the bookmark ID, the tag *name*, or the record ID.
    ``group`` ties together changes that only make sense together; the user
    accepts or rejects whole groups. ``title`` and ``note`` are what the
    preview shows.
    """

    entity: str
    entity_id: str
    field: str
    old: str | None
    new: str | None
    group: str
    title: str = ""
    note: str = ""

    def key(self) -> tuple:
        return (self.entity, self.entity_id, self.field, self.old, self.new, self.group)


@dataclass
class Script:
    name: str
    title: str
    description: str
    version: str
    params: list[dict]
    module: ModuleType
    builtin: bool
    path: str | None = None

    def info(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "params": self.params,
            "builtin": self.builtin,
        }


# ---- finding scripts -------------------------------------------------------


def _wrap(module: ModuleType, *, builtin: bool, path: str | None = None) -> Script:
    name = getattr(module, "NAME", None)
    if not isinstance(name, str) or not name or not callable(getattr(module, "plan", None)):
        raise GroomError("a script needs a NAME and a plan(conn, params) function")
    return Script(
        name=name,
        title=getattr(module, "TITLE", name),
        description=(getattr(module, "DESCRIPTION", "") or "").strip(),
        version=str(getattr(module, "VERSION", "1")),
        params=list(getattr(module, "PARAMS", [])),
        module=module,
        builtin=builtin,
        path=path,
    )


def user_scripts_dir(db_path: Path | str) -> Path:
    """Folder for the user's own scripts: ``scripts`` beside the database."""
    return Path(db_path).resolve().parent / "scripts"


def load_scripts(db_path: Path | str | None = None) -> tuple[dict[str, Script], list[dict]]:
    """Built-in scripts plus the user's. Returns ``(scripts, problems)``.

    User scripts are loaded only from the folder beside the database. A script
    that fails to load is reported in ``problems`` and skipped; it never stops
    the others.
    """
    scripts: dict[str, Script] = {}
    problems: list[dict] = []
    for module_name in BUILTIN:
        script = _wrap(importlib.import_module(f"atlas.groom.{module_name}"), builtin=True)
        scripts[script.name] = script

    folder = user_scripts_dir(db_path) if db_path is not None else None
    if folder is not None and folder.is_dir():
        for path in sorted(folder.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"atlas_user_{path.stem}", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                script = _wrap(module, builtin=False, path=str(path))
                if script.name in scripts:
                    raise GroomError(f"the name {script.name!r} is already taken")
                scripts[script.name] = script
            except Exception as error:  # a broken user script must not break the app
                problems.append({"file": path.name, "error": f"{type(error).__name__}: {error}"})
    return scripts, problems


def get_script(name: str, db_path: Path | str | None = None) -> Script:
    scripts, _ = load_scripts(db_path)
    if name not in scripts:
        raise GroomError(f"No grooming script named {name!r}.")
    return scripts[name]


# ---- planning --------------------------------------------------------------


def _clean_params(script: Script, given: dict | None) -> dict:
    given = dict(given or {})
    cleaned = {}
    for spec in script.params:
        name = spec["name"]
        value = given.pop(name, spec.get("default"))
        kind = spec.get("type", "text")
        if kind == "bool":
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            value = bool(value)
        elif kind == "choice":
            if value not in [choice["value"] for choice in spec.get("choices", [])]:
                raise GroomError(f"{value!r} is not a valid value for {name}.")
        elif kind == "int":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise GroomError(f"{name} must be a whole number.") from None
        else:
            value = "" if value is None else str(value)
        cleaned[name] = value
    if given:
        raise GroomError(f"Unknown parameter: {', '.join(sorted(given))}.")
    return cleaned


def _validate(change: Change) -> None:
    ok = (
        (change.entity == "bookmark" and change.field in BOOKMARK_FIELDS)
        or (
            change.entity == "bookmark_tag"
            and change.field == "tag"
            and (change.old is None) != (change.new is None)
        )
        or (change.entity == "tag" and change.field == "alias_of" and change.new is not None)
        or (
            change.entity == "source_record"
            and change.field == "pending"
            and {change.old, change.new} == {"0", "1"}
        )
    )
    if not ok:
        raise GroomError(
            f"The script proposed a change the engine does not allow: "
            f"{change.entity}.{change.field} ({change.old!r} -> {change.new!r})."
        )
    if not change.group:
        raise GroomError("The script proposed a change without a group.")


def fingerprint(changes: list[Change]) -> str:
    encoded = json.dumps([change.key() for change in changes], ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def plan(conn: sqlite3.Connection, script: Script, params: dict | None = None):
    """Run the script. Returns ``(cleaned params, changes, fingerprint)``. Writes nothing."""
    cleaned = _clean_params(script, params)
    before = conn.total_changes
    changes = list(script.module.plan(conn, cleaned))
    if conn.total_changes != before or conn.in_transaction:
        conn.rollback()
        raise GroomError("The script tried to write to the database while planning.")
    seen = set()
    for change in changes:
        if not isinstance(change, Change):
            raise GroomError("The script must return Change objects.")
        _validate(change)
        if change.key() in seen:
            raise GroomError("The script proposed the same change twice.")
        seen.add(change.key())
    return cleaned, changes, fingerprint(changes)


def _describe(change: Change) -> str:
    if change.note:
        return change.note
    if change.entity == "bookmark_tag":
        return f"add tag “{change.new}”" if change.old is None else f"remove tag “{change.old}”"
    if change.entity == "tag":
        return f"tag “{change.entity_id}” becomes an alias of “{change.new}”"
    if change.entity == "source_record":
        return "mark the duplicate record as handled"
    old = "empty" if change.old is None else change.old
    new = "empty" if change.new is None else change.new
    return f"{change.field}: {old} → {new}"


def group_changes(changes: list[Change], limit: int | None = None) -> list[dict]:
    """Changes gathered by group, in first-seen order, ready to display."""
    groups: dict[str, dict] = {}
    for change in changes:
        group = groups.setdefault(change.group, {"key": change.group, "title": "", "lines": []})
        if change.title and not group["title"]:
            group["title"] = change.title
        group["lines"].append(_describe(change))
    ordered = list(groups.values())
    for group in ordered:
        group["title"] = group["title"] or group["key"]
    return ordered if limit is None else ordered[:limit]


def preview(
    conn: sqlite3.Connection, script: Script, params: dict | None = None, *, limit: int = 300
) -> dict:
    cleaned, changes, mark = plan(conn, script, params)
    groups = group_changes(changes)
    return {
        "script": script.name,
        "params": cleaned,
        "fingerprint": mark,
        "changes": len(changes),
        "groups_total": len(groups),
        "groups": groups[:limit],
    }


# ---- writing ---------------------------------------------------------------


def _tag_id(conn: sqlite3.Connection, name: str, *, create: bool) -> int | None:
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row[0]
    if not create:
        return None
    return conn.execute("INSERT INTO tags (name) VALUES (?)", (name,)).lastrowid


def _write(conn: sqlite3.Connection, change: Change, *, reverse: bool = False) -> None:
    """Apply one change, or its reverse. Refuses if the current value is not the expected one."""
    old, new = (change.new, change.old) if reverse else (change.old, change.new)

    def conflict(found) -> GroomError:
        return GroomError(
            f"The data changed in the meantime ({change.entity} {change.entity_id}, "
            f"{change.field}: expected {old!r}, found {found!r}). Nothing was written."
        )

    if change.entity == "bookmark":
        if change.field not in BOOKMARK_FIELDS:
            raise GroomError(f"Field {change.field!r} cannot be changed.")
        row = conn.execute(
            f"SELECT {change.field} FROM bookmarks WHERE id = ?", (change.entity_id,)
        ).fetchone()
        if row is None or row[0] != old:
            raise conflict(row[0] if row else "no such bookmark")
        conn.execute(
            f"UPDATE bookmarks SET {change.field} = ?, updated_at = ? WHERE id = ?",
            (new, now_iso(), change.entity_id),
        )

    elif change.entity == "bookmark_tag":
        name = new if old is None else old
        tag_id = _tag_id(conn, name, create=old is None)
        present = (
            tag_id is not None
            and conn.execute(
                "SELECT 1 FROM bookmark_tags WHERE bookmark_id = ? AND tag_id = ?",
                (change.entity_id, tag_id),
            ).fetchone()
        )
        if old is None:  # add
            if present:
                raise conflict(f"tag {name!r} already present")
            conn.execute(
                "INSERT INTO bookmark_tags (bookmark_id, tag_id) VALUES (?, ?)",
                (change.entity_id, tag_id),
            )
        else:  # remove
            if not present:
                raise conflict(f"tag {name!r} absent")
            conn.execute(
                "DELETE FROM bookmark_tags WHERE bookmark_id = ? AND tag_id = ?",
                (change.entity_id, tag_id),
            )

    elif change.entity == "tag":
        row = conn.execute(
            "SELECT t.id, a.name FROM tags t LEFT JOIN tags a ON a.id = t.alias_of "
            "WHERE t.name = ?",
            (change.entity_id,),
        ).fetchone()
        if row is None or row[1] != old:
            raise conflict(row[1] if row else "no such tag")
        target = None if new is None else _tag_id(conn, new, create=True)
        conn.execute("UPDATE tags SET alias_of = ? WHERE id = ?", (target, row[0]))

    elif change.entity == "source_record":
        row = conn.execute(
            "SELECT pending FROM source_records WHERE id = ?", (int(change.entity_id),)
        ).fetchone()
        if row is None or str(row[0]) != old:
            raise conflict(row[0] if row else "no such record")
        conn.execute(
            "UPDATE source_records SET pending = ? WHERE id = ?", (int(new), int(change.entity_id))
        )

    else:
        raise GroomError(f"Unknown kind of change: {change.entity!r}.")


def backup_database(conn: sqlite3.Connection, db_path: Path | str, label: str) -> Path:
    """Copy the database into ``backups/`` beside it, keeping the most recent few."""
    folder = Path(db_path).resolve().parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "")
    target = folder / f"atlas-{stamp}-{label}-{uuid.uuid4().hex[:6]}.sqlite"
    copy = sqlite3.connect(target)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    backups = sorted(folder.glob("atlas-*.sqlite"), key=lambda path: path.stat().st_mtime)
    for stale in backups[:-KEEP_BACKUPS]:
        stale.unlink(missing_ok=True)
    return target


def apply(
    conn: sqlite3.Connection,
    script: Script,
    params: dict | None = None,
    *,
    expected_fingerprint: str | None = None,
    exclude: set[str] | None = None,
    db_path: Path | str | None = None,
) -> dict:
    """Run the script and write the accepted groups. All or nothing.

    ``expected_fingerprint`` is the one a preview returned: if the script would
    now propose something else, the run is refused so that what gets written is
    what the user saw.
    """
    cleaned, changes, mark = plan(conn, script, params)
    if expected_fingerprint is not None and expected_fingerprint != mark:
        raise GroomError("The collection changed since the preview. Preview again, then apply.")
    exclude = exclude or set()
    accepted = [change for change in changes if change.group not in exclude]
    return apply_changes(
        conn,
        accepted,
        label=script.name,
        version=script.version,
        params=cleaned,
        db_path=db_path,
        skipped_groups=len(exclude & {change.group for change in changes}),
    )


def apply_changes(
    conn: sqlite3.Connection,
    accepted: list[Change],
    *,
    label: str,
    version: str = "1",
    params: dict | None = None,
    db_path: Path | str | None = None,
    skipped_groups: int = 0,
) -> dict:
    """Write a list of changes as one journaled, undoable run. All or nothing.

    Scripts go through ``apply``; edits made by hand in the interface come
    straight here, so they get the same backup, journal and undo.
    """
    for change in accepted:
        _validate(change)
    if not accepted:
        return {"run_id": None, "applied": 0, "groups": 0, "backup": None}

    backup = backup_database(conn, db_path, label) if db_path is not None else None
    run_id = uuid.uuid4().hex
    now = now_iso()
    groups = len({change.group for change in accepted})
    try:
        conn.execute(
            "INSERT INTO script_runs (id, script, script_version, params, started_at, status, "
            "summary) VALUES (?, ?, ?, ?, ?, 'applied', ?)",
            (
                run_id,
                label,
                version,
                json.dumps(params or {}, ensure_ascii=False),
                now,
                json.dumps(
                    {
                        "changes": len(accepted),
                        "groups": groups,
                        "skipped_groups": skipped_groups,
                        "backup": backup.name if backup else None,
                    }
                ),
            ),
        )
        for change in accepted:
            if change.new == NOW:
                change = replace(change, new=now)
            _write(conn, change)
            conn.execute(
                "INSERT INTO changes (script_run_id, entity, entity_id, field, old_value, "
                "new_value, applied_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    change.entity,
                    change.entity_id,
                    change.field,
                    change.old,
                    change.new,
                    now,
                ),
            )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return {
        "run_id": run_id,
        "applied": len(accepted),
        "groups": groups,
        "backup": str(backup) if backup else None,
    }


def undo(conn: sqlite3.Connection, run_id: str, *, db_path: Path | str | None = None) -> dict:
    """Reverse an applied run, newest change first. All or nothing."""
    run = conn.execute("SELECT * FROM script_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise GroomError("No such run.")
    if run["status"] != "applied":
        raise GroomError("This run has already been undone.")
    rows = conn.execute(
        "SELECT * FROM changes WHERE script_run_id = ? ORDER BY id DESC", (run_id,)
    ).fetchall()
    if db_path is not None:
        backup_database(conn, db_path, f"undo-{run['script']}")
    try:
        for row in rows:
            change = Change(
                row["entity"],
                row["entity_id"],
                row["field"],
                row["old_value"],
                row["new_value"],
                "-",
            )
            _write(conn, change, reverse=True)
        conn.execute("UPDATE script_runs SET status = 'undone' WHERE id = ?", (run_id,))
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return {"run_id": run_id, "reverted": len(rows)}


def history(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    runs = []
    for row in conn.execute(
        "SELECT * FROM script_runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
    ):
        summary = json.loads(row["summary"] or "{}")
        runs.append(
            {
                "run_id": row["id"],
                "script": row["script"],
                "started_at": row["started_at"],
                "status": row["status"],
                "params": json.loads(row["params"] or "{}"),
                "changes": summary.get("changes", 0),
                "groups": summary.get("groups", 0),
            }
        )
    return runs
