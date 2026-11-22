"""Edge cases an independent review found by experiment. Each of these once
crashed, lost data or gave a wrong result."""

import io

import pytest

from atlas import db, importer, sqldump
from atlas.analysis import store
from atlas.dates import parse_date
from atlas.groom import published_from_x_id
from atlas.server import App
from test_links_trends import add


@pytest.fixture
def conn(tmp_path):
    made = db.connect(tmp_path / "a.sqlite", create=True)
    db.migrate(made)
    yield made
    made.close()


@pytest.mark.parametrize("count", [2, 3, 5, 30])
def test_a_map_of_bookmarks_that_have_no_words(conn, count):
    """Links imported bare (no title, no summary, no tags) gave vectors with a
    single dimension, and drawing the map failed on an index."""
    for i in range(count):
        add(conn, f"https://example.org/{i}", "", "", [])
    conn.commit()
    store.cluster_run(conn)
    drawn = store.project_run(conn)
    assert drawn["points"] == count


def test_identical_bookmarks_still_get_a_map(conn):
    for i in range(40):
        add(conn, f"https://example.org/{i}", "Same title", "same words every time", ["one"])
    conn.commit()
    store.cluster_run(conn)
    assert store.project_run(conn)


def test_one_bookmark_alone_says_what_is_missing(conn):
    add(conn, "https://example.org/only", "Only one", "alone", [])
    conn.commit()
    with pytest.raises(ValueError, match="at least two bookmarks"):
        store.cluster_run(conn)
    assert store.candidate_ks(3) == [2] and store.candidate_ks(2) == [2]
    assert max(store.candidate_ks(40)) <= 40


PLAIN_DUMP = """-- MySQL dump 10.13
/*!40101 SET NAMES utf8 */;
DROP TABLE IF EXISTS `links`;
CREATE TABLE `links` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `url` varchar(255) NOT NULL,
  `title` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;
INSERT INTO `links` VALUES (1,'https://example.org/one','One'),(2,'https://example.org/two','It''s two');
"""


def test_a_dump_written_without_column_names_is_read():
    """What mysqldump writes by default: the columns are those of CREATE TABLE."""
    assert sqldump.looks_like_sql_dump(PLAIN_DUMP)
    rows = sqldump.read_tables(PLAIN_DUMP)["links"]
    assert rows == [
        {"id": 1, "url": "https://example.org/one", "title": "One"},
        {"id": 2, "url": "https://example.org/two", "title": "It's two"},
    ]


def test_a_dump_cut_short_says_so():
    with pytest.raises(ValueError, match="cut short"):
        sqldump.read_tables(PLAIN_DUMP[: PLAIN_DUMP.index("'It''s")] + "'It")


def test_a_csv_that_mentions_sql_is_still_a_csv(tmp_path):
    text = (
        "URL,Name,AI summary\n"
        'https://example.org/sql,"Learn SQL","From CREATE TABLE to INSERT INTO in ten steps"\n'
    )
    assert not sqldump.looks_like_sql_dump(text)
    assert not sqldump.looks_like_sql_dump('{"note": "CREATE TABLE x; INSERT INTO x"}')
    path = tmp_path / "b.sqlite"
    made = db.connect(path, create=True)
    db.migrate(made)
    made.close()
    report = App(path).import_csv({"content": text, "filename": "links.csv"})["report"]
    assert report["created"] == 1


def test_a_csv_cell_holding_a_whole_article_is_read(conn):
    text = 'URL,Name,AI summary\nhttps://example.org/long,"Long","' + "word " * 40_000 + '"\n'
    report = importer.import_csv_stream(conn, io.StringIO(text, newline=""))
    assert report.created == 1


def test_dates_at_the_edge_of_the_calendar_are_unreadable_not_fatal():
    assert parse_date("0001-01-01T00:00:00+05:00") is None
    assert parse_date("9999-12-31T23:59:59-05:00") is None
    assert parse_date("2024-02-03T10:00:00+02:00").iso.startswith("2024-02-03")
    assert published_from_x_id.published_at("9" * 29) is None
    assert published_from_x_id.published_at("1800000000000000000") is not None


def test_a_question_is_not_compared_with_vectors_of_another_model(conn, monkeypatch):
    """Vectors of two models do not compare: with the same size the closest
    bookmarks were noise presented as "meaning", with another size a crash."""
    import numpy as np

    from atlas import assist

    for i in range(6):
        add(conn, f"https://example.org/{i}", f"Rivers {i}", "rivers maps cartography", ["maps"])
    conn.commit()
    ids = [row[0] for row in conn.execute("SELECT id FROM bookmarks ORDER BY id")]
    monkeypatch.setattr(
        assist, "_raw_model_vectors", lambda c: (ids, np.eye(6, 8, dtype=np.float32))
    )
    monkeypatch.setattr(
        assist,
        "vector_status",
        lambda c: {"ready": 6, "total": 6, "model": "embeddinggemma-2/768/v1"},
    )

    class Other:
        def __init__(self, contract, size):
            self.contract, self.size = contract, size

        def embedding(self):
            return {"contract": self.contract}

        def embed(self, texts):
            return [[1.0] * self.size]

    assert (
        assist.find_for_question(conn, Other("embeddinggemma-2/768/v1", 8), "rivers maps")[1]
        == "meaning"
    )
    assert (
        assist.find_for_question(conn, Other("another-model/8/v1", 8), "rivers maps")[1] == "words"
    )
    assert (
        assist.find_for_question(conn, Other("embeddinggemma-2/768/v1", 5), "rivers maps")[1]
        == "words"
    )
