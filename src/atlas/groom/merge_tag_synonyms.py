"""Merge tags that say the same thing in another form."""

import re

from atlas.groom.normalize_tags import merge_families, spelling_key

NAME = "merge-tag-synonyms"
TITLE = "Merge tag synonyms"
DESCRIPTION = """
Goes one step further than "Merge tag spellings": a plural and its singular
become one tag (Workflow, Workflows), and so do initials and the words they
stand for when both exist (3DGS, 3D Gaussian Splatting). These are guesses
from the shape of the words: read each proposal and untick the wrong ones.
"""
VERSION = "1"
PARAMS = []


def singular(key: str) -> str:
    """A spelling key without its plural ending."""
    if len(key) > 4 and key.endswith("ies"):
        return key[:-3] + "y"
    if len(key) > 4 and key.endswith("s") and not key.endswith(("ss", "us", "is")):
        return key[:-1]
    return key


def initials(name: str) -> str | None:
    """Initials of a tag of three words or more: 3D Gaussian Splatting -> 3dgs."""
    words = [word for word in re.split(r"[\s\-_/]+", name) if word]
    if len(words) < 3:
        return None
    letters = [word if any(c.isdigit() for c in word) else word[0] for word in words]
    return "".join(letters).casefold()


def plan(conn, params):
    names = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT t.name FROM tags t JOIN bookmark_tags bt ON bt.tag_id = t.id "
            "WHERE t.alias_of IS NULL ORDER BY t.name"
        )
    ]
    keys = {name: singular(spelling_key(name)) for name in names}
    present = set(keys.values())
    # Initials join the family of the words they stand for, when exactly one tag fits.
    stands_for: dict[str, set[str]] = {}
    for name in names:
        short = initials(name)
        if short and short in present and short != keys[name]:
            stands_for.setdefault(short, set()).add(keys[name])
    redirect = {short: next(iter(long)) for short, long in stands_for.items() if len(long) == 1}
    return merge_families(conn, lambda name: redirect.get(keys[name], keys[name]))
