"""Merge the tags a language model judged to mean the same thing."""

from atlas.groom.normalize_tags import merge_families

NAME = "merge-tags-from-model"
TITLE = "Merge tags that mean the same (language model)"
DESCRIPTION = """
Goes further than spellings and plurals: a tag and its translation, a synonym,
an abbreviation and its full form become one tag ("dataviz", "data
visualisation", "visualisation de données"). The groups come from "Find tags
that mean the same" above; this script does not call the model. The most used
spelling stays and the others become its aliases. A model can be wrong about
meaning: read each proposal and untick the ones that should stay apart.
"""
VERSION = "1"
PARAMS = []


def plan(conn, params):
    family = dict(conn.execute("SELECT tag, family FROM llm_tag_groups"))
    return merge_families(
        conn, lambda name: family.get(name.casefold()), " (same meaning, says the model)"
    )
