# ruff: noqa: E501
"""Run the built-in engine for real, once, on the build machine.

It downloads the light model and the vectors model as the app would, starts
llama-server with them, and asks one notebook question about three invented
bookmarks. It proves the plumbing with the real programs and files: the
download addresses, the server's flags, its key, the form imposed on the
answer, the vectors. It does not judge the quality of the answer.

    python engine-selfcheck.py <engine folder> <folder of the atlas package's parent>
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

scratch = Path(tempfile.mkdtemp())
os.environ["ATLAS_ENGINE_DIR"] = str(Path(sys.argv[1]).resolve())
os.environ["ATLAS_MODELS_DIR"] = str(scratch / "models")
os.environ["ATLAS_CONFIG_DIR"] = str(scratch / "config")
os.environ["ATLAS_NO_KEYRING"] = "1"
sys.path.insert(0, str(Path(sys.argv[2]).resolve()))

from atlas import db, engine, llm, notebook  # noqa: E402
from atlas.importer import ImportReport, import_record  # noqa: E402

PAGES = {
    "https://example.org/danube": (
        "The Danube",
        "The Danube is the second longest river in Europe. It rises in the Black Forest in Germany "
        "and crosses ten countries before it reaches the Black Sea through a wide delta in Romania.",
    ),
    "https://example.org/sourdough": (
        "Sourdough at home",
        "A sourdough starter needs only flour and water. Fed every day, it is ready to raise a "
        "loaf after about one week. Steam in the oven gives the bread a thin, crisp crust.",
    ),
    "https://example.org/typography": (
        "Choosing a typeface",
        "A text typeface is judged at reading size, not in a headline. Generous spacing and open "
        "counters keep small text legible on a screen.",
    ),
}


def step(title):
    print(f"\n== {title}", flush=True)


step("engine")
print(json.dumps({k: v for k, v in engine.state().items() if k != "models"}))
assert engine.state()["available"], "llama-server was not found"

for key in ("light", "vectors"):
    step(f"download {key}: {engine.CATALOGUE[key]['url']}")
    started = time.time()
    engine.download(key)
    while engine._downloads.get(key, {}).get("status") in ("downloading", "checking"):
        time.sleep(2)
    status = engine._downloads.get(key, {"status": "already there"})
    print(
        status.get("status"),
        status.get("error") or "",
        f"{time.time() - started:.0f} s",
        f"{engine.model_path(key).stat().st_size / 1e6:.0f} MB"
        if engine.model_path(key).exists()
        else "",
    )
    assert engine.model_path(key).is_file(), f"{key} did not arrive"

step("collection")
conn = db.connect(scratch / "atlas.sqlite", create=True)
db.migrate(conn)
report = ImportReport()
ids = []
for url, (title, text) in PAGES.items():
    import_record(
        conn,
        report,
        source="csv",
        raw={},
        url=url,
        title=title,
        summary=None,
        tags=[],
        date_text=None,
    )
    bookmark = conn.execute("SELECT id FROM bookmarks WHERE url = ?", (url,)).fetchone()[0]
    ids.append(bookmark)
    conn.execute(
        "INSERT INTO page_texts (bookmark_id, url, fetched_at, status, title, text) "
        "VALUES (?, ?, '2026-01-01T00:00:00Z', 'ok', ?, ?)",
        (bookmark, url, title, text),
    )
conn.commit()
print(len(ids), "bookmarks")

step("start the writer and ask for one word")
connection = llm.Connection(
    {"kind": "local", "base_url": "local", "chat_model": "light"}, password=""
)
started = time.time()
print(
    repr(
        connection.chat(
            [{"role": "user", "content": "Answer with the single word: ready"}], max_tokens=20
        )
    ),
    f"{time.time() - started:.0f} s",
)

step("vectors")
where = connection.embedding()
vector = connection.embed([llm.query_text(where["contract"], "a river in Europe")])[0]
print(where["contract"], len(vector), "numbers")
assert len(vector) == 768, "the vectors model did not give 768 numbers"

step("a notebook question")
started = time.time()
told = notebook.answer(
    conn, connection, ids, "Which countries does the Danube cross, and where does it end?"
)
print(
    f"{time.time() - started:.0f} s · found by {told['found_by']} · {len(told['statements'])} "
    f"statements kept, {told['dropped']} set aside"
)
for statement in told["statements"]:
    print("  -", statement["text"], f"[{statement['passage']}]", "<<", statement["quote"])
print("  missing:", told["missing"])
print("  first passage:", told["passages"][0]["title"])
assert told["found_by"] == "meaning and words", "passages were not found by meaning"
assert told["passages"][0]["title"] == "The Danube", "the closest passage is not the right one"
sent_form = connection._json_mode
print("  the form was imposed by a schema:", sent_form)
engine.stop()
print("\nself-check passed")
