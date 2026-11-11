# Phase summaries

One short summary per phase, written when the phase is published. Details are
in [DEVLOG.md](DEVLOG.md); the road ahead is in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

| Phase | Subject | Release | State |
| --- | --- | --- | --- |
| 0 | Scaffolding | v0.2.0 | done |
| 1 | Local store and import | v0.2.0 | done, Notion token import untried with a real token |
| 2 | Grooming, batch edits, sharing | v0.2.0, v0.2.1 | done, four planned scripts still to write |
| 3 | Vectors, clusters and the e-means test | v0.3.0 | done |
| 4 | Map view | | next |
| 5 | Saved views and snapshots | | |
| 6 | Self-organising map and pearl tree | | |
| 7 | Links and tendencies | | |

## Phase 0: scaffolding

- A Python package, `atlas`, with a command line. Standard library only.
- One SQLite file holds everything; schema changes are numbered migrations.
- Tests and lint run on every push, on Linux and Windows.

## Phase 1: local store and import

- **Atlas is the reference.** Notion and CSV files are ways in. What arrives is
  kept untouched beside the curated bookmark and never overwrites it.
- **Import rules:** a new link becomes a bookmark; a known link is kept aside
  for review; a deleted bookmark stays deleted; importing twice changes
  nothing.
- **Links are compared after normalisation** (x.com and twitter.com, tracking
  parameters, YouTube and GitHub variants).
- **Notion:** token stored outside the database, list of tables, guessed
  column mapping, incremental sync. Tested against a stand-in only.
- **Desktop app:** Electron shell with a bundled Python, unsigned Windows
  installer, self-update from GitHub Releases. The release workflow starts the
  packaged app before publishing it.
- **Checked on the real export:** 962 rows give 956 bookmarks and 6 duplicates.

## Phase 2: grooming, batch edits, sharing

- **Scripts propose, the app writes.** Preview, pick groups, apply. A backup is
  made first, every change is journaled, any run can be undone exactly.
- **Five scripts:** merge duplicates, merge tag spellings, platform from the
  link, publication date of X posts, dates without a time.
- **Your own scripts** go in a folder beside the database and get the same
  safety net. See [GROOMING.md](GROOMING.md).
- **Batch edits:** select bookmarks (or everything matching a search), add or
  remove a tag, delete. Each batch is one undoable run.
- **Sharing:** export a selection as a bundle file; another user imports it.
  A bundle holds links, titles, summaries, tags, platform and publication
  date, and nothing else.
- **Checked on the real export:** 66 tag spellings merged, all 956 platforms
  set, all 489 X posts dated, 133 other dates stored; undoing everything
  returns the exact starting state.
- **What the data taught:** a Date without a time is the publication day on X
  rows (304 of 306 match the post ID). On other rows it comes from the tagging
  tool and is not reliable.
- **Portable data folder (0.2.1):** everything the user owns is in one folder
  that can be placed anywhere and copied to another machine.
- **Left for later:** similar-title duplicates, tag synonyms, publication
  dates fetched from the platforms, dead-link check, review screen for rows
  edited in Notion.

## Phase 3: vectors, clusters and the e-means test

- **Vectors without a model download.** Each bookmark becomes a vector from
  the words of its title, summary and tags (latent semantic analysis, NumPy
  only). Rebuilt automatically when the text changes.
- **Clusters tab.** Auto or chosen number of clusters, three methods, cluster
  cards with telling tags and sample bookmarks, rename, and "show bookmarks"
  which filters the list so a whole cluster can be selected, tagged or
  exported.
- **e-means is the reference code.** The NumPy class from the Faiss repository
  is vendored unmodified; only its nearest-centroid search is replaced by an
  exact NumPy one.
- **Verdict on e-means:** equal to k-means at this size (losses within about
  1% either way, same repeatability). Its advantage is speed on data a
  thousand times larger. k-means stays the default; e-means is selectable.
  Report: [reports/e-means-test.md](reports/e-means-test.md).
- **What mattered more than the algorithm:** the vectors. Using 16 directions
  gave separated, repeatable clusters (stability 0.8); using 128 gave mush
  (0.4).
- **On the real collection:** auto picks 16 clusters, and they read as topics.
- **Left for later:** a neural embedding model, which should separate topics
  better and would need a download.
