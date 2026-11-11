import pytest

from atlas.urls import domain_of, normalize_url, x_status_id


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://www.example.org/page/", "http://example.org/page"),
        ("https://example.org/p?utm_source=x&id=3", "https://example.org/p?id=3"),
        ("https://example.org/p?b=2&a=1", "https://example.org/p?a=1&b=2"),
        ("https://example.org/p#section", "https://example.org/p"),
        ("https://twitter.com/Someone/status/123", "https://x.com/someone/status/123?s=20"),
        ("https://x.com/a/status/123/photo/1", "https://mobile.twitter.com/b/status/123"),
        ("https://youtu.be/abc123", "https://m.youtube.com/watch?v=abc123&t=10s"),
        ("https://github.com/Owner/Repo.git", "https://github.com/owner/repo/"),
        ("example.org/page", "https://example.org/page"),
    ],
)
def test_equivalent_urls_share_a_key(a, b):
    assert normalize_url(a) == normalize_url(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://example.org/a", "https://example.org/b"),
        ("https://example.org/p?id=1", "https://example.org/p?id=2"),
        ("https://x.com/a/status/123", "https://x.com/a/status/124"),
        ("https://youtube.com/watch?v=abc", "https://youtube.com/watch?v=abd"),
        ("https://example.org/Page", "https://example.org/page"),
        ("https://github.com/owner/repo", "https://github.com/owner/repo/issues/1"),
    ],
)
def test_different_content_keeps_different_keys(a, b):
    assert normalize_url(a) != normalize_url(b)


def test_domain_resolves_aliases():
    assert domain_of("https://www.twitter.com/a/status/1") == "x.com"
    assert domain_of("https://m.youtube.com/watch?v=a") == "youtube.com"
    assert domain_of("https://youtu.be/a") == "youtube.com"
    assert domain_of("https://Blog.Example.org/x") == "blog.example.org"


def test_x_status_id():
    assert x_status_id("https://x.com/a/status/2030000000000000001?s=20") == "2030000000000000001"
    assert x_status_id("https://x.com/a") is None
    assert x_status_id("https://example.org/a/status/12") is None


@pytest.mark.parametrize("bad", ["", "   ", "mailto:a@example.org", "https://", "localhost"])
def test_non_web_urls_are_rejected(bad):
    with pytest.raises(ValueError):
        normalize_url(bad)
