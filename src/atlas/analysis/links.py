"""Links between bookmarks, and what they reveal.

Two bookmarks are linked when one is among the other's nearest neighbours in
the text vectors. From those links:

- **related**: the neighbours of one bookmark, with the tags they share;
- **topic links**: how many links cross from one topic to another;
- **bridges**: bookmarks whose neighbours mostly belong to another topic;
- **outliers**: bookmarks that resemble nothing else in the collection;
- **worth another look**: old bookmarks that recent ones point to.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, timedelta

from atlas.analysis import require_numpy, store

NEIGHBOURS = 10


def _nearest(np, matrix, k: int):
    """Indices and similarities of the ``k`` nearest rows of each row."""
    similarity = matrix @ matrix.T
    np.fill_diagonal(similarity, -np.inf)
    k = max(1, min(k, len(matrix) - 1))
    index = np.argsort(-similarity, axis=1, kind="stable")[:, :k]
    return index, np.take_along_axis(similarity, index, axis=1)


def _load(conn: sqlite3.Connection):
    np = require_numpy()
    ids, matrix, _ = store.load_vectors(conn)
    matrix = store.leading(matrix).astype(np.float64)
    rows = {
        row["id"]: row
        for row in conn.execute(
            "SELECT id, title, url, domain, saved_at, published_at FROM bookmarks "
            "WHERE deleted_at IS NULL"
        )
    }
    return np, ids, matrix, rows


def _brief(row) -> dict:
    return {"id": row["id"], "title": row["title"] or row["url"], "url": row["url"]}


def related(conn: sqlite3.Connection, bookmark_id: str, limit: int = 8) -> dict:
    """The bookmarks closest to one bookmark."""
    np, ids, matrix, rows = _load(conn)
    if bookmark_id not in rows or bookmark_id not in ids:
        raise ValueError("This bookmark is not in the collection.")
    if len(ids) < 2:
        return {"bookmark": _brief(rows[bookmark_id]), "related": []}
    position = ids.index(bookmark_id)
    similarity = matrix @ matrix[position]
    similarity[position] = -np.inf
    order = np.argsort(-similarity, kind="stable")[: max(1, min(limit, 30))]
    _, _, _, tags = store._documents(conn)
    own = set(tags.get(bookmark_id, []))
    found = []
    for other in order:
        row = rows[ids[other]]
        found.append(
            {
                **_brief(row),
                "similarity": round(float(similarity[other]), 3),
                "shared_tags": sorted(own & set(tags.get(row["id"], []))),
                "same_site": bool(row["domain"]) and row["domain"] == rows[bookmark_id]["domain"],
            }
        )
    return {"bookmark": _brief(rows[bookmark_id]), "related": found}


def overview(conn: sqlite3.Connection, run_id: str | None = None) -> dict:
    """Topic links, bridges and outliers, for a clustering run (the latest by default)."""
    np, ids, matrix, rows = _load(conn)
    run_id = run_id or store.latest_run_id(conn)
    if run_id is None:
        return {"cluster_run": None, "topics": [], "links": [], "bridges": [], "outliers": []}
    described = store.describe_run(conn, run_id, samples=0)
    names = {item["cluster"]: item["label"] for item in described["clusters"]}
    membership = dict(
        conn.execute("SELECT bookmark_id, cluster FROM run_items WHERE run_id = ?", (run_id,))
    )
    keep = [i for i, bookmark_id in enumerate(ids) if bookmark_id in membership]
    ids = [ids[i] for i in keep]
    matrix = matrix[keep]
    empty = {"cluster_run": run_id, "topics": [], "links": [], "bridges": [], "outliers": []}
    if len(ids) < 3:
        return empty
    labels = np.array([membership[b] for b in ids])
    index, similarity = _nearest(np, matrix, NEIGHBOURS)
    k = index.shape[1]

    # Links between topics: pairs of bookmarks, each counted once.
    pairs = set()
    for i in range(len(ids)):
        for j in index[i]:
            pairs.add((min(i, int(j)), max(i, int(j))))
    crossing: Counter = Counter()
    for i, j in pairs:
        if labels[i] != labels[j]:
            crossing[tuple(sorted((int(labels[i]), int(labels[j]))))] += 1
    sizes = Counter(int(n) for n in labels)
    links = [
        {
            "a": a,
            "b": b,
            "links": count,
            # Links per possible pair of bookmarks, so that large topics do not win by size.
            "strength": round(count / (sizes[a] * sizes[b]) ** 0.5, 3),
        }
        for (a, b), count in crossing.items()
    ]
    links.sort(key=lambda item: (-item["strength"], item["a"], item["b"]))

    # Bridges: most neighbours belong to one other topic.
    bridges = []
    for i in range(len(ids)):
        elsewhere = Counter(int(labels[j]) for j in index[i] if labels[j] != labels[i])
        if not elsewhere:
            continue
        other, count = elsewhere.most_common(1)[0]
        share = sum(elsewhere.values()) / k
        if share >= 0.5 and count >= max(2, k // 3):
            bridges.append(
                {
                    **_brief(rows[ids[i]]),
                    "topic": int(labels[i]),
                    "towards": other,
                    "share": round(share, 2),
                }
            )
    bridges.sort(key=lambda item: (-item["share"], item["title"].lower()))

    # Outliers: even the nearest neighbours are far.
    closeness = similarity.mean(axis=1)
    order = np.argsort(closeness, kind="stable")[: min(15, max(1, len(ids) // 20))]
    outliers = [
        {
            **_brief(rows[ids[i]]),
            "topic": int(labels[i]),
            "closeness": round(float(closeness[i]), 3),
        }
        for i in order
    ]
    return {
        "cluster_run": run_id,
        "neighbours": k,
        "topics": [
            {"cluster": n, "label": names.get(n, f"Topic {n}"), "size": sizes[n]}
            for n in sorted(sizes)
        ],
        "links": links,
        "bridges": bridges[:25],
        "outliers": outliers,
    }


def day_of(row) -> date | None:
    """The day a bookmark entered the picture: saved, or else published."""
    for value in (row["saved_at"], row["published_at"]):
        if value:
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                continue
    return None


def resurface(
    conn: sqlite3.Connection, recent_days: int = 45, old_days: int = 180, limit: int = 15
) -> dict:
    """Old bookmarks that the recent ones resemble.

    "Recent" and "old" are counted back from the latest date in the collection,
    not from today, so a collection left alone for a while still has an answer.
    """
    np, ids, matrix, rows = _load(conn)
    days = [day_of(rows[b]) for b in ids]
    dated = [d for d in days if d is not None]
    if not dated:
        return {"latest": None, "items": [], "recent": 0, "old": 0}
    latest = max(dated)
    recent = [i for i, d in enumerate(days) if d and d > latest - timedelta(days=recent_days)]
    old = [i for i, d in enumerate(days) if d and d <= latest - timedelta(days=old_days)]
    result = {
        "latest": latest.isoformat(),
        "recent_days": recent_days,
        "old_days": old_days,
        "recent": len(recent),
        "old": len(old),
        "items": [],
    }
    if not recent or not old:
        return result
    similarity = matrix[recent] @ matrix[old].T  # (recent, old)
    best = similarity.argmax(axis=1)
    called: dict[int, list[tuple[float, int]]] = {}
    for r, o in enumerate(best):
        score = float(similarity[r, o])
        if score >= 0.6:
            called.setdefault(int(o), []).append((score, recent[r]))
    items = []
    for o, callers in called.items():
        callers.sort(reverse=True)
        items.append(
            {
                **_brief(rows[ids[old[o]]]),
                "day": days[old[o]].isoformat(),
                "similarity": round(callers[0][0], 3),
                "echoes": len(callers),
                "because": [_brief(rows[ids[i]]) for _, i in callers[:3]],
            }
        )
    items.sort(key=lambda item: (-item["echoes"], -item["similarity"], item["title"].lower()))
    result["items"] = items[:limit]
    return result
