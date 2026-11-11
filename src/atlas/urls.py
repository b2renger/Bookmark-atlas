"""URL normalisation.

Two links that point to the same thing should produce the same normalised
form, which is what imports match on and what duplicate detection compares.
The original URL is always kept as is; the normalised form is only a key.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

# Hosts that are the same site under another name.
HOST_ALIASES = {
    "twitter.com": "x.com",
    "mobile.twitter.com": "x.com",
    "mobile.x.com": "x.com",
    "m.youtube.com": "youtube.com",
    "music.youtube.com": "youtube.com",
}

# Query parameters that track the visitor and never change the content.
TRACKING_PARAMS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref_src", "ref_url"}
TRACKING_PREFIXES = ("utm_",)

_X_STATUS = re.compile(r"^/[^/]+/status(?:es)?/(\d+)")


def _split(url: str):
    url = url.strip()
    if not url:
        raise ValueError("empty URL")
    if "://" not in url:
        # "example.org/page" is accepted, "mailto:someone" is not. A colon
        # followed by a digit is a port, as in "example.org:8080".
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:(?!\d)", url):
            raise ValueError(f"not a web URL: {url!r}")
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"not a web URL: {url!r}")
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        raise ValueError(f"no host in URL: {url!r}")
    if host.startswith("www."):
        host = host[4:]
    return HOST_ALIASES.get(host, host), parts


def domain_of(url: str) -> str:
    """Site the URL belongs to, without ``www.`` and with aliases resolved."""
    host, _ = _split(url)
    return "youtube.com" if host == "youtu.be" else host


def x_status_id(url: str) -> str | None:
    """ID of the X post a URL points to, or None if it is not a post link."""
    try:
        host, parts = _split(url)
    except ValueError:
        return None
    if host != "x.com":
        return None
    match = _X_STATUS.match(parts.path)
    return match.group(1) if match else None


def normalize_url(url: str) -> str:
    """Key under which equivalent URLs collide. Not meant to be opened.

    The scheme and fragment are dropped, tracking parameters removed and the
    remaining parameters sorted. A few sites get their own rules because their
    links come in several shapes for the same content.
    """
    host, parts = _split(url)
    path = parts.path
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS and not key.lower().startswith(TRACKING_PREFIXES)
    ]

    if host == "x.com":
        match = _X_STATUS.match(path)
        if match:
            # The post ID alone identifies the post, whatever the account
            # name, trailing /photo/1 or share parameters.
            return f"x.com/i/status/{match.group(1)}"
        path = path.lower()
        query = [(k, v) for k, v in query if k not in ("s", "t")]

    elif host == "youtu.be":
        video = path.strip("/").split("/")[0]
        if video:
            return f"youtube.com/watch?v={video}"
        host = "youtube.com"

    elif host == "youtube.com" and path.rstrip("/") == "/watch":
        video = dict(query).get("v")
        if video:
            return f"youtube.com/watch?v={video}"

    elif host == "github.com":
        # Owner and repository names are case-insensitive on GitHub.
        segments = path.split("/")
        for index in (1, 2):
            if index < len(segments):
                segments[index] = segments[index].lower()
        path = "/".join(segments)
        if path.endswith(".git"):
            path = path[:-4]

    path = path.rstrip("/")
    normalized = host + path
    if query:
        normalized += "?" + urlencode(sorted(query))
    return normalized
