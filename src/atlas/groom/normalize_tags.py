"""Merge tags that differ only by capitals, hyphens, dots or spacing."""

import re
from collections import Counter, defaultdict

from atlas.groom import Change

NAME = "normalize-tags"
TITLE = "Merge tag spellings"
DESCRIPTION = """
Merges tags that are the same word written differently: "Computer Vision" and
"computer-vision", "Three.js" and "Threejs". The spelling used most often
wins. Tags that mean the same thing but are written with different words
("3DGS" and "Gaussian Splatting") are not touched.
"""
VERSION = "1"
PARAMS = []

# Characters that do not distinguish two tags. "+" and "#" are kept, so "C",
# "C++" and "C#" stay apart.
_IGNORED = re.compile(r"[\s\-_./]+")


def spelling_key(name: str) -> str:
    return _IGNORED.sub("", name).casefold()


def _preference(item):
    name, count = item
    # Most used first; then a spelling with spaces, then one with capitals.
    return (-count, " " not in name, name == name.lower(), name)


def plan(conn, params):
    usage = Counter()
    holders = defaultdict(list)
    for name, bookmark_id in conn.execute(
        "SELECT t.name, bt.bookmark_id FROM bookmark_tags bt JOIN tags t ON t.id = bt.tag_id "
        "WHERE t.alias_of IS NULL ORDER BY bt.bookmark_id"
    ):
        usage[name] += 1
        holders[name].append(bookmark_id)

    families = defaultdict(list)
    for name, count in usage.items():
        key = spelling_key(name)
        if key:
            families[key].append((name, count))

    changes = []
    for _key, members in sorted(families.items()):
        if len(members) < 2:
            continue
        members.sort(key=_preference)
        keeper = members[0][0]
        others = [name for name, _ in members[1:]]
        group = f"tag:{keeper}"
        title = f"{keeper}  ←  {', '.join(others)}"
        has_keeper = set(holders[keeper])
        for other in others:
            for bookmark_id in holders[other]:
                changes.append(
                    Change(
                        "bookmark_tag", bookmark_id, "tag", other, None, group=group, title=title
                    )
                )
                if bookmark_id not in has_keeper:
                    has_keeper.add(bookmark_id)
                    changes.append(
                        Change(
                            "bookmark_tag",
                            bookmark_id,
                            "tag",
                            None,
                            keeper,
                            group=group,
                            title=title,
                        )
                    )
            changes.append(Change("tag", other, "alias_of", None, keeper, group=group, title=title))
    return changes
