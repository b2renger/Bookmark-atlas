# Bookmark Atlas

A desktop app to explore and refine a bookmark collection: clustering, maps,
nested collections and trends, built to encourage discovery.

Atlas is the final database. Bookmarks (with summaries and tags) arrive from a
Notion table, which is only an inbox and an optional outbox. The local SQLite
file is the reference, and it is cleaned with grooming scripts run from the
interface.

It is also a test bed for **e-means**, a clustering algorithm published in
November 2026 ([arXiv 2611.09501](https://arxiv.org/abs/2611.09501)).

## Status

Phase 1 of 7. A first usable build exists: a local interface to connect Notion,
pick a table, import it and browse the result. Grooming, clustering and the
visual views come next.

**The Notion import has not been run against the real Notion API yet.** It is
tested against a stand-in built from Notion's documentation. Use Preview before
the first import and report what it shows.

- Where development stands: [docs/DEVLOG.md](docs/DEVLOG.md)
- Where it is going: [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)

## Run it

Needs Python 3.11 or later and nothing else.

```sh
git clone https://github.com/b2renger/Bookmark-atlas.git
cd Bookmark-atlas
python3 -m pip install -e .
atlas ui
```

Without installing: `PYTHONPATH=src python3 -m atlas ui` (on Windows,
`set PYTHONPATH=src` first, then `python -m atlas ui`).

`atlas ui` creates `data/atlas.sqlite` if needed, starts the interface on this
computer only and opens it in the browser. Stop it with Ctrl+C.

### Connect Notion

In **Settings**:

1. **Token.** Create one in Notion's Developer portal and paste it. Either kind
   works:
   - a *personal access token* sees what you can see, with nothing to share;
   - an *internal connection* token sees only what is shared with the
     connection. Share the bookmarks database with it, from the connection's
     Content access tab or from the database's ••• menu under Connections.

   The token is checked with Notion before it is stored, and it is stored on
   your computer outside the database file.
2. **Table.** List the tables the token can see and pick yours.
3. **Columns.** Check the guessed mapping. Only the link column is required.
4. **Import.** *Preview* shows what would happen without writing anything.
   *Import now* does it. Later imports only ask Notion for rows edited since.

### What an import does

- A link not seen before becomes a bookmark.
- A row edited in Notion later is kept for review. It never overwrites what is
  in the database.
- A bookmark you deleted stays deleted.
- Importing again changes nothing if Notion has not changed.

Links are compared after normalisation, so `twitter.com` and `x.com`, tracking
parameters, or a trailing slash do not create duplicates.

### Command line

Everything the interface does is also a command:

```sh
atlas init                              # create or upgrade the database
atlas import-csv export.csv --dry-run   # a Notion CSV export, report only
atlas import-csv export.csv
atlas notion token                      # enter the token (input hidden)
atlas notion tables                     # list tables with their IDs
atlas notion select <table-id>
atlas sync --dry-run
atlas sync                              # --full reads the whole table again
atlas status
atlas export --format json -o bookmarks.json
```

The database path is `data/atlas.sqlite`, or `$ATLAS_DB`, or `--db PATH`. The
`data/` folder is ignored by git, so bookmarks never end up in the repository.
The token can also be given as the `NOTION_TOKEN` environment variable.

## Development

```sh
python -m pytest
ruff check . && ruff format --check .
```

## Planned shape

- **Import**: incremental import from Notion through its API, or from a CSV
  export. Imports never overwrite local curation.
- **Local store**: one SQLite file, exportable to CSV and JSON.
- **Grooming**: scripts for duplicates, tags and dates, each with preview,
  journal and undo.
- **Analysis**: embeddings, e-means (with k-means as a baseline), 2D projection,
  self-organising map.
- **Views**: map, SOM grid, pearl tree, link graph, tendencies over time.
- **Snapshots**: saved runs and views, exportable as a single shareable file.
