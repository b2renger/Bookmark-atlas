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


def distinct_names(groups: dict, tags: dict) -> dict:
    """A name for each group of bookmarks that tells it apart from the others.

    ``groups`` maps a key to bookmark IDs. A tag that many groups carry says
    little about any of them, so tags are weighed by how few groups have them,
    and a name already given to a larger group is not given again while the
    group has another tag that describes it. Returns ``{key: [name, more...]}``:
    the name first, then up to two other telling tags.
    """
    counts = {key: Counter(t for b in ids for t in tags.get(b, [])) for key, ids in groups.items()}
    spread = Counter(tag for counter in counts.values() for tag in counter)
    total = max(len(groups), 1)
    taken: set[str] = set()
    names = {}
    for key in sorted(groups, key=lambda k: (-len(groups[k]), str(k))):
        size = len(groups[key])
        if not size or not counts[key]:
            names[key] = []
            continue
        ranked = sorted(
            counts[key].items(),
            key=lambda item: (
                -(item[1] / size) * math.log(1.0 + total / spread[item[0]]),
                -item[1],
                item[0],
            ),
        )
        # A name must describe the group: carried by a fifth of it, and not by one bookmark only.
        enough = max(2, math.ceil(size * 0.2)) if size > 2 else 1
        solid = [tag for tag, count in ranked if count >= enough]
        fresh = [tag for tag in solid if tag not in taken]
        if fresh:
            name = fresh[0]
        elif solid:
            # Nothing of its own: say the shared tag, and the next thing that sets it apart.
            other = next((tag for tag, _ in ranked if tag != solid[0] and tag not in taken), None)
            name = f"{solid[0]} · {other}" if other else solid[0]
        else:
            name = next((tag for tag, _ in ranked if tag not in taken), ranked[0][0])
        taken.add(name)
        taken.update(part for part in name.split(" · "))
        rest = [tag for tag, _ in ranked if tag not in name.split(" · ")][:2]
        names[key] = [name, *rest]
    return names


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


def _resize(tree: dict) -> None:
    """Recount every node from its leaves, drop what became empty, sort by size."""

    def count(node: dict) -> int:
        if node.get("children"):
            node["children"] = [child for child in node["children"] if count(child) > 0]
            node["children"].sort(key=lambda child: -child["size"])
        if "ids" in node:
            node["size"] = len(node["ids"])
        else:
            node["size"] = sum(child["size"] for child in node.get("children", []))
        return node["size"]

    count(tree)


def merge_topics(tree: dict, keep_id: str, other_id: str) -> None:
    """Fold one topic into another. The kept topic keeps its name and family;
    its parts are dropped, since they no longer describe it."""
    keep, _ = find(tree, keep_id)
    other, other_parent = find(tree, other_id)
    if keep is None or other is None or keep["kind"] != "topic" or other["kind"] != "topic":
        raise ValueError("Only two topics can be merged.")
    if keep is other:
        raise ValueError("A topic cannot be merged with itself.")
    keep["ids"] = keep["ids"] + [b for b in other["ids"] if b not in set(keep["ids"])]
    keep["children"] = []
    keep["edited"] = True
    other_parent["children"].remove(other)
    _resize(tree)


def merge_families(tree: dict, keep_id: str, other_id: str) -> None:
    """Move every topic of one family into another."""
    keep, _ = find(tree, keep_id)
    other, _ = find(tree, other_id)
    if keep is None or other is None or keep["kind"] != "family" or other["kind"] != "family":
        raise ValueError("Only two families can be merged.")
    if keep is other:
        raise ValueError("A family cannot be merged with itself.")
    keep["children"].extend(other["children"])
    keep["edited"] = True
    tree["children"].remove(other)
    _resize(tree)


def split_topic(tree: dict, topic_id: str) -> None:
    """Turn the parts of a topic into topics of their own, in the same family."""
    topic, family = find(tree, topic_id)
    if topic is None or topic["kind"] != "topic":
        raise ValueError("Only a topic can be split.")
    if len(topic.get("children") or []) < 2:
        raise ValueError("This topic has no parts to split into.")
    for part in topic["children"]:
        part["kind"] = "topic"
        part["children"] = []
        part["edited"] = True
    family["children"].remove(topic)
    family["children"].extend(topic["children"])
    _resize(tree)


def new_family(tree: dict, topic_id: str) -> str:
    """Give a topic a family of its own. Returns the new family's ID."""
    topic, old = find(tree, topic_id)
    if topic is None or topic["kind"] != "topic":
        raise ValueError("Only a topic can start a family.")
    if len(old["children"]) == 1:
        raise ValueError("This topic is already alone in its family.")
    taken = {child["id"] for child in tree["children"]}
    number = next(n for n in range(len(taken) + 1) if f"g{n}" not in taken)
    old["children"].remove(topic)
    tree["children"].append(
        {
            "id": f"g{number}",
            "kind": "family",
            "name": topic["name"],
            "size": 0,
            "children": [topic],
            "edited": True,
        }
    )
    _resize(tree)
    return f"g{number}"
