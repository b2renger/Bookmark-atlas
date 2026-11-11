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

Phase 2 of 7. Import (Notion or CSV), browse, and groom the collection with
scripts. Clustering and the visual views come next.

**The Notion import has not been run against the real Notion API with a real
token yet.** It is tested against a stand-in built from Notion's documentation.
Use Preview before the first import and report what it shows. The CSV import
has been checked on a real 962-row export.

- Summary of each phase: [docs/PHASES.md](docs/PHASES.md)
- Where development stands: [docs/DEVLOG.md](docs/DEVLOG.md)
- Where it is going: [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)

## Install (Windows)

1. Open the [latest release](https://github.com/b2renger/Bookmark-atlas/releases/latest)
   and download `Bookmark-Atlas-Setup-<version>-windows-x64.exe`.
2. Run it. The installer is not signed, so Windows shows "Windows protected
   your PC" the first time: choose **More info**, then **Run anyway**.
3. The app opens. Nothing else to install: Python is included.

The app updates itself. It checks GitHub Releases at start, downloads a new
version in the background and offers to restart. **Atlas > Check for updates…**
checks on demand.

Your bookmarks are in `%APPDATA%\Bookmark Atlas\atlas.sqlite`
(**Atlas > Open data folder**). Updates and uninstalling leave that folder
alone.

macOS and Linux builds are not produced yet.

## Use it

In **Settings** you can load data two ways, and combine them:

- **Import a CSV file**: an export of the table from Notion. No token needed.
- **Notion**: connect with a token, pick the table, import. Later imports only
  fetch rows edited since.

In **Bookmarks**, tick rows (or "select all matching" a search) to add or
remove a tag, delete, or **export the selection** as a file another Atlas user
can import from Settings.

Then clean it in **Grooming**: merge duplicates, merge tag spellings, set
platforms and fix dates. Every script shows a preview first, makes a backup
before writing, and can be undone. You can add your own scripts: see
[docs/GROOMING.md](docs/GROOMING.md).

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

## For developers

Needs Python 3.11 or later; the backend uses the standard library only.

```sh
python3 -m pip install -e .
atlas ui                                # the interface, in your browser
python -m pytest
ruff check . && ruff format --check .
```

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
atlas groom list                        # grooming scripts
atlas groom preview normalize-tags      # what it would change
atlas groom apply normalize-tags
atlas groom undo last
atlas doctor                            # check the installation and Notion access
```

The database path is `data/atlas.sqlite`, or `$ATLAS_DB`, or `--db PATH`. The
`data/` folder is ignored by git, so bookmarks never end up in the repository.
The token can also be given as the `NOTION_TOKEN` environment variable.

### Desktop app and releases

`desktop/` is an Electron shell. It starts the Python backend, shows its page
in a window and updates itself with electron-updater, the same way ComfyQ and
LlmOnLan do. The installer bundles a standalone Python, so users install
nothing else.

- **Release:** in the Actions tab, run the `release` workflow and type a
  version such as `0.3.0`. It runs the tests, builds the installer, starts the
  packaged app to check it works, creates the tag and publishes the release
  with the update manifest. Pushing a tag `vX.Y.Z` does the same.
- **Trial build:** run the workflow with the version left empty. The installer
  is attached to the run and nothing is published.
- Self-update reads public GitHub Releases, so it needs the repository to stay
  public.

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
