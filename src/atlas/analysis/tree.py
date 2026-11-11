"""The topic tree: clusters grouped into families and split into sub-topics.

Three levels under the root:

- **families**: topics whose centres are close are joined (Ward's method on
  the cluster centres), until a handful of families remain;
- **topics**: the clusters of a clustering run, with their labels;
- **sub-topics**: a large topic is split again with k-means.

The tree is stored as data and can be edited: nodes can be renamed and a
topic moved to another family. Editing never recomputes anything.
"""

from __future__ import annotations

import math
from collections import Counter

from atlas.analysis import cluster, require_numpy

SPLIT_FROM = 24  # topics with at least this many bookmarks get sub-topics


def ward_groups(np, centroids, sizes, groups: int) -> list[list[int]]:
    """Join the closest centres, by Ward's criterion, until ``groups`` remain."""
    members = [[i] for i in range(len(centroids))]
    centres = [np.asarray(c, dtype=np.float64) for c in centroids]
    weights = [float(s) for s in sizes]
    while len(members) > max(1, groups):
        best = None
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                gap = centres[i] - centres[j]
                cost = weights[i] * weights[j] / (weights[i] + weights[j]) * float(gap @ gap)
                if best is None or cost < best[0]:
                    best = (cost, i, j)
        _, i, j = best
        total = weights[i] + weights[j]
        centres[i] = (centres[i] * weights[i] + centres[j] * weights[j]) / total
        weights[i] = total
        members[i] = members[i] + members[j]
        del members[j], centres[j], weights[j]
    return [sorted(group) for group in members]


def telling_tags(tags: dict, ids: list[str], against: Counter, against_total: int, limit: int = 3):
    """Tags frequent among ``ids`` and rarer in the reference set."""
    inside = Counter(tag for bookmark_id in ids for tag in tags.get(bookmark_id, []))
    scored = sorted(
        inside.items(),
        key=lambda item: (
            -item[1]
            * math.log(
                1.0 + (item[1] / max(len(ids), 1)) / (against[item[0]] / max(against_total, 1))
            ),
            item[0],
        ),
    )
    return [name for name, _ in scored[:limit]]


def build(ids, matrix, labels, centroids, tags: dict, topic_labels: dict, seed: int = 0) -> dict:
    """The tree for a clustering: ``ids`` and rows of ``matrix`` are aligned,
    ``labels`` gives each row's cluster, ``topic_labels`` each cluster's name."""
    np = require_numpy()
    labels = np.asarray(labels)
    numbers = sorted(int(n) for n in np.unique(labels))
    rows_of = {n: np.flatnonzero(labels == n) for n in numbers}
    overall = Counter(tag for bookmark_id in ids for tag in tags.get(bookmark_id, []))

    wanted = max(2, min(len(numbers), round(math.sqrt(len(numbers)))))
    families = ward_groups(
        np, [centroids[n] for n in numbers], [len(rows_of[n]) for n in numbers], wanted
    )

    def topic_node(number: int) -> dict:
        rows = rows_of[number]
        topic_ids = [ids[i] for i in rows]
        node = {
            "id": f"t{number}",
            "kind": "topic",
            "name": topic_labels.get(number) or f"Topic {number}",
            "size": len(topic_ids),
            "ids": topic_ids,
            "children": [],
        }
        if len(rows) >= SPLIT_FROM:
            parts = min(4, max(2, len(rows) // 12))
            sub_labels, _, _ = cluster.fit(matrix[rows], parts, algorithm="k-means", seed=seed)
            inside = Counter(tag for b in topic_ids for tag in tags.get(b, []))
            for part in range(parts):
                part_ids = [topic_ids[i] for i in np.flatnonzero(sub_labels == part)]
                if not part_ids:
                    continue
                names = telling_tags(tags, part_ids, inside, len(topic_ids))
                node["children"].append(
                    {
                        "id": f"t{number}.{part}",
                        "kind": "subtopic",
                        "name": " · ".join(names) or f"Part {part + 1}",
                        "size": len(part_ids),
                        "ids": part_ids,
                        "children": [],
                    }
                )
            node["children"].sort(key=lambda child: -child["size"])
        return node

    children = []
    for index, family in enumerate(families):
        topics = sorted((topic_node(numbers[i]) for i in family), key=lambda t: -t["size"])
        family_ids = [b for topic in topics for b in topic["ids"]]
        names = telling_tags(tags, family_ids, overall, len(ids))
        children.append(
            {
                "id": f"g{index}",
                "kind": "family",
                "name": " · ".join(names) or f"Family {index + 1}",
                "size": len(family_ids),
                "children": topics,
            }
        )
    children.sort(key=lambda child: -child["size"])
    return {
        "id": "root",
        "kind": "root",
        "name": "All bookmarks",
        "size": len(ids),
        "children": children,
    }


def find(node: dict, node_id: str, parent: dict | None = None):
    """``(node, parent)`` for an ID, or ``(None, None)``."""
    if node["id"] == node_id:
        return node, parent
    for child in node.get("children", []):
        found, found_parent = find(child, node_id, node)
        if found is not None:
            return found, found_parent
    return None, None


def rename(tree: dict, node_id: str, name: str) -> None:
    node, _ = find(tree, node_id)
    if node is None or node["kind"] == "root":
        raise ValueError("No such item in the tree.")
    name = " ".join((name or "").split())[:120]
    if not name:
        raise ValueError("Give it a name.")
    node["name"] = name
    node["edited"] = True


def move_topic(tree: dict, topic_id: str, family_id: str) -> None:
    """Move a topic to another family. Sizes are kept right; an emptied family is removed."""
    topic, old = find(tree, topic_id)
    family, _ = find(tree, family_id)
    if topic is None or topic["kind"] != "topic":
        raise ValueError("Only a topic can be moved.")
    if family is None or family["kind"] != "family":
        raise ValueError("A topic can only be moved into a family.")
    if old is family:
        return
    old["children"].remove(topic)
    old["size"] -= topic["size"]
    family["children"].append(topic)
    family["children"].sort(key=lambda child: -child["size"])
    family["size"] += topic["size"]
    if not old["children"]:
        tree["children"].remove(old)
    tree["children"].sort(key=lambda child: -child["size"])
