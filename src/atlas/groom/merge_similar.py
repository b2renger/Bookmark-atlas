"""Find bookmarks that are the same thing under two links."""

from atlas.analysis import text
from atlas.groom.common import label, live_bookmarks
from atlas.groom.merge_duplicates import absorb

NAME = "merge-similar"
TITLE = "Merge near-duplicates"
DESCRIPTION = """
Two bookmarks with different links can still be the same thing: the same
article saved from two addresses, a post and its repost. This script compares
titles and summaries word by word and proposes to merge those that nearly
match; the oldest one stays. It is a guess: read each proposal before
applying, and untick what is merely similar.
"""
VERSION = "1"
PARAMS = [
    {
        "name": "strictness",
        "type": "choice",
        "default": "strict",
        "label": "How alike two bookmarks must be",
        "choices": [
            {"value": "strict", "label": "nearly identical title and summary"},
            {"value": "loose", "label": "close title and summary (more proposals, more mistakes)"},
        ],
    }
]

LEVELS = {"strict": (0.85, 0.75), "loose": (0.7, 0.55)}
MAX_BOOKMARKS = 6000  # every pair is compared; beyond this it would take minutes


def _overlap(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def plan(conn, params):
    title_level, summary_level = LEVELS[params["strictness"]]
    bookmarks = live_bookmarks(conn)[:MAX_BOOKMARKS]
    titles = [frozenset(text.tokenize(b["title"] or "")) for b in bookmarks]
    summaries = [frozenset(text.tokenize(b["summary"] or "")) for b in bookmarks]

    parent = list(range(len(bookmarks)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    scores = {}
    for i in range(len(bookmarks)):
        if len(titles[i]) < 3:
            continue  # a title of one or two words says too little
        for j in range(i + 1, len(bookmarks)):
            if (
                len(titles[j]) < 3
                or bookmarks[i]["url_normalized"] == bookmarks[j]["url_normalized"]
            ):
                continue
            title = _overlap(titles[i], titles[j])
            if title < title_level:
                continue
            both = summaries[i] and summaries[j]
            summary = _overlap(summaries[i], summaries[j]) if both else None
            if summary is not None and summary < summary_level:
                continue
            if summary is None and title < 1.0:
                continue  # without summaries to compare, only an identical title counts
            scores[(i, j)] = (title, summary)
            parent[find(j)] = find(i)

    families: dict[int, list[int]] = {}
    for i in range(len(bookmarks)):
        families.setdefault(find(i), []).append(i)
    changes = []
    for members in families.values():
        if len(members) < 2:
            continue
        keeper, *others = [bookmarks[i] for i in members]  # oldest first
        absorb(
            conn,
            changes,
            keeper,
            others,
            why=f"looks like the same thing (kept: “{label(keeper)[:60]}”) and is merged into it",
        )
    return changes
