"""Fill the platform of each bookmark from the site its link points to."""

from atlas.groom import Change
from atlas.groom.common import label, live_bookmarks

NAME = "derive-platform"
TITLE = "Set platform from the link"
DESCRIPTION = """
Names the platform of each bookmark from its site: X, GitHub, Hugging Face,
YouTube and a few others, and "Website" for everything else. The Platform
column from Notion is not used: it is empty on most rows.
"""
VERSION = "1"
PARAMS = [
    {
        "name": "overwrite",
        "type": "bool",
        "default": False,
        "label": "Also replace platforms that are already set",
    }
]

EXACT = {
    "x.com": "X",
    "github.com": "GitHub",
    "gist.github.com": "GitHub",
    "huggingface.co": "Hugging Face",
    "youtube.com": "YouTube",
    "arxiv.org": "arXiv",
    "reddit.com": "Reddit",
    "old.reddit.com": "Reddit",
    "civitai.com": "Civitai",
    "linkedin.com": "LinkedIn",
    "medium.com": "Medium",
    "vimeo.com": "Vimeo",
    "instagram.com": "Instagram",
    "bsky.app": "Bluesky",
    "threads.net": "Threads",
    "threads.com": "Threads",
    "news.ycombinator.com": "Hacker News",
}
SUFFIX = {
    ".github.io": "GitHub Pages",
    ".substack.com": "Substack",
    ".medium.com": "Medium",
    ".hf.space": "Hugging Face",
    ".notion.site": "Notion",
}
DEFAULT = "Website"


def platform_for(domain: str) -> str:
    if domain in EXACT:
        return EXACT[domain]
    for suffix, name in SUFFIX.items():
        if domain.endswith(suffix):
            return name
    return DEFAULT


def plan(conn, params):
    changes = []
    for bookmark in live_bookmarks(conn):
        wanted = platform_for(bookmark["domain"])
        current = bookmark["platform"]
        if current == wanted or (current is not None and not params["overwrite"]):
            continue
        changes.append(
            Change(
                "bookmark",
                bookmark["id"],
                "platform",
                current,
                wanted,
                group=f"platform:{wanted}",
                title=wanted,
                note=f"{label(bookmark)} ({bookmark['domain']})",
            )
        )
    return sorted(changes, key=lambda change: change.group != f"platform:{DEFAULT}")
