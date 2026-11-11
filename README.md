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

Phase 1 of 7, in progress. The local store, CSV import and export work from
the command line. There is no desktop interface yet.

- Where development stands: [docs/DEVLOG.md](docs/DEVLOG.md)
- Where it is going: [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)

## Try it

Needs Python 3.11 or later. Nothing to install: this phase uses the standard
library only.

```sh
export PYTHONPATH=src            # or: pip install -e .

python -m atlas init                               # creates data/atlas.sqlite
python -m atlas import-csv export.csv --dry-run    # report only
python -m atlas import-csv export.csv
python -m atlas status
python -m atlas export --format json -o bookmarks.json
```

`export.csv` is a Notion table exported as CSV. The importer looks for columns
named Name, URL, Description, Keywords and Date. Other names can be given with
`--column`, for example `--column url=Link`.

The database path is `data/atlas.sqlite`, or `$ATLAS_DB`, or `--db PATH`. The
`data/` folder is ignored by git, so bookmarks never end up in the repository.

### What an import does

- A link not seen before becomes a bookmark.
- A link already known is kept as a source record for review. It never
  overwrites what is in the database.
- A bookmark you deleted stays deleted.
- Importing the same file again changes nothing.

Links are compared after normalisation, so `twitter.com` and `x.com`, tracking
parameters, or a trailing slash do not create duplicates.

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
