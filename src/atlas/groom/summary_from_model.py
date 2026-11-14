"""Replace or fill summaries with the ones a language model wrote."""

from atlas.groom import Change
from atlas.groom.common import label

NAME = "summary-from-model"
TITLE = "Summaries written by the language model"
DESCRIPTION = """
Uses the summaries written by "Write summaries and tags" above, from the text
of each page as it reads today. This script does not go online and does not
call the model; run the two steps above first. By default it only fills
bookmarks that have no summary, or one that was cut short (it ends with …).
Choose "every bookmark" to replace the others too: the preview shows the old
and the new text side by side, and the run can be undone.
"""
VERSION = "1"
PARAMS = [
    {
        "name": "which",
        "label": "Which bookmarks",
        "type": "choice",
        "choices": [
            {"value": "missing", "label": "missing or cut short"},
            {"value": "all", "label": "every bookmark"},
        ],
        "default": "missing",
    }
]


def plan(conn, params):
    everything = params.get("which") == "all"
    changes = []
    rows = conn.execute(
        "SELECT b.*, n.value AS written, n.model AS written_by FROM bookmarks b "
        "JOIN llm_notes n ON n.bookmark_id = b.id AND n.kind = 'summary' "
        "JOIN page_texts p ON p.bookmark_id = b.id AND p.url = b.url "
        "WHERE b.deleted_at IS NULL AND n.value <> '' ORDER BY b.created_at, b.id"
    ).fetchall()
    for bookmark in rows:
        current = (bookmark["summary"] or "").strip()
        if current == bookmark["written"]:
            continue
        if current and not current.endswith("…") and not everything:
            continue
        changes.append(
            Change(
                "bookmark",
                bookmark["id"],
                "summary",
                bookmark["summary"],
                bookmark["written"],
                group=f"bookmark:{bookmark['id']}",
                title=label(bookmark),
                note=("replaces: " + current[:160] + " → " if current else "new: ")
                + bookmark["written"][:300]
                + f" (written by {bookmark['written_by'] or 'the model'})",
            )
        )
    return changes
