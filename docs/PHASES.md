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
| 4 | Map view | v0.4.0 | done |
| 5 | Saved views and snapshots | v0.5.0 | done |
| 6 | Self-organising map and pearl tree | v0.6.0 | done, drag and drop, merge and split left out |
| 7 | Links and tendencies | v0.7.1 | done (v0.7.0 was built from the wrong commit) |
| 8 | Shared filter, animated layouts, timeline, 3D | v0.8.0 | done for list, map and grid |
| 9 | Publication dates read from the pages | v0.9.0 | done; not yet run against real sites |

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

## Phase 4: the map

- **A 2D map of the collection.** t-SNE, written with NumPy, places similar
  bookmarks next to each other. On the real collection its layout is as
  faithful as scikit-learn's (trustworthiness 0.989 for both; a plain PCA
  gives 0.78) and takes about 6 seconds.
- **Topics are named, not coloured.** Labels sit on the map at the centre of
  each cluster. Three colours, checked for colour-blind safety on both light
  and dark backgrounds, show state: focused topic, search matches, selection.
- **Linked to everything else.** Hover and click for details; Shift+drag
  selects an area into the same selection the list uses, so a region of the
  map can be tagged, exported, or shown in the list.
- **Same map every time** for the same collection; new bookmarks are reported
  as missing until the map is redrawn.
- **Left for later:** filters by tag, platform and date on the map; a faster
  layout for collections beyond a few thousand bookmarks (they fall back to a
  cruder projection today).

## Phase 5: saved views and snapshots

- **Saved views.** A view stores the tab, search, cluster filter, selection
  and map position and focus, with a name and a note. Opening it restores
  them.
- **Snapshots.** One database file with the bookmarks in scope, tags,
  clusters, map, vectors and saved views. Private material is left out:
  source records, grooming journal, sync state.
- **A snapshot is an ordinary Atlas database**, so there is no second file
  format to maintain. Opening one copies it into its own folder and switches
  the app to it; the menu brings you back.
- **Two kinds of sharing.** A bundle (phase 2) adds bookmarks to the
  receiver's collection. A snapshot shows an exploration as it was.
- **Checked on the real collection:** a view of the map with 132 selected
  bookmarks, a focus and a highlight survives a reload; a 132-bookmark
  snapshot is 454 kB and opens in a second Atlas with its banner, its map and
  the sender's view.
- **Left for later:** comparing two snapshots, a read-only web page export of
  the map, notes attached to single bookmarks.

## Phase 6: self-organising map and pearl tree

- **Grid tab.** A self-organising map trained in NumPy (batch training, started
  from the two main directions of the data, so the same collection gives the
  same grid). About a dozen bookmarks per cell. Disc area is the number of
  bookmarks; each cell is named after its most telling tag.
- **The grid answers a different question from the map:** the map shows which
  bookmarks are alike, the grid shows where the collection is dense and which
  themes border which.
- **Tree tab.** Three levels: families (close topics joined by Ward's method),
  topics (the clusters), parts (a topic of 24 or more is split again). Shown
  as pearls you open, with a path back.
- **The tree is data you can edit:** rename a family, topic or part; move a
  topic to another family. Edits are stored and travel in snapshots. Building
  the tree again starts over.
- **Colour still means state, not topic:** on the grid, green marks cells with
  a search match and orange the share of a cell that is selected.
- **Checked on the real collection:** 8 x 11 grid in 0.2 s, no empty cell, 95%
  of bookmarks have their two closest cells side by side; the tree gives 4
  families for 16 topics, in 0.5 s.
- **Left out:** drag and drop, merging and splitting by hand, a grid for a
  filtered part of the collection.

## Phase 7: links and tendencies

- **Links tab.** Two bookmarks are linked when one is among the other's ten
  nearest. Topics are drawn on a circle, joined where they share many links
  (each topic's two strongest, so the picture stays readable).
- **Three lists come out of the links:** bridges (filed in one topic, most
  neighbours in another), bookmarks on their own, and old bookmarks that
  recent ones resemble.
- **"Similar bookmarks"** under every bookmark in the list.
- **Trends tab.** Saved and published per month over the last 36 months, time
  between publication and saving, topics and tags rising or fading (last 90
  days against everything before, as shares of each period).
- **Dates are counted back from the latest date in the collection,** not from
  today, so a snapshot shows the same figures to whoever opens it.
- **The tab says what its figures rest on:** how many bookmarks have each
  date, and how many were placed by publication date for lack of a save date.
- **Checked on the real collection:** 16 topics, 25 bridges, 15 old bookmarks
  worth another look; 184 bookmarks have both dates and half were saved within
  6 days of publication. 438 bookmarks have no save date yet, so the rising and
  fading figures will shift once Notion supplies them.
- **Left out:** a graph of single bookmarks, choice of the periods compared.

## Phase 8: shared filter, animated layouts, timeline, 3D

- **One filter bar for the list, the map and the grid:** tags (all must
  match), site, a range of dates, and which date is used. On the map,
  bookmarks left out become small and faint and cannot be picked; the grid
  counts only what passes.
- **Four arrangements of the same dots on the map,** with a 0.9 s glide
  between them: similarity, timeline (one row per topic), grid, and space and
  time in 3D (the map lies flat, time is the height, drag to turn).
- **Play and a time slider:** only what existed on a given day is shown, in
  any arrangement.
- **One mechanism under all of it:** every arrangement places each dot in a
  unit cube and a camera looks at the cube, from above for the flat ones. A
  change of arrangement is one animation of positions and camera.
- **No library added.** The package registry cannot be reached from the
  development session, so regl-scatterplot and three.js could not be tried.
  At under a thousand dots the existing canvas redraws every frame without
  effort; WebGL becomes worth it in the tens of thousands.
- **The time axis starts at the third percentile of dates;** older bookmarks
  sit in an "earlier" strip, so a few old ones do not squeeze the rest.
- **Saved views remember the filter and the arrangement.**
- **Left out:** the filter does not yet apply to Tree, Links and Trends; no
  brush on a histogram to pick dates; no 3D link graph.

## Phase 9: publication dates read from the pages

- **Grooming, "Look up publication dates":** goes online, page by page, for
  bookmarks without a publication date, and reads the date the page declares
  (structured data, then meta tags, then a marked `<time>` element). For a
  GitHub repository: the day it was created, from GitHub's public API.
- **The lookup changes no bookmark.** It fills a table (`page_facts`). The
  script "Publication date read from the page" then proposes the dates, with
  the usual preview, apply and undo.
- **Why two steps:** a script is run twice (preview, then apply) and must
  propose the same thing both times; the web does not promise that.
- **Nothing is guessed.** A date written only in the text of a page is not
  used. A page that declares none is recorded as such and not asked again.
- **Progress, Stop, and "try again the pages that could not be reached".**
  When GitHub asks to slow down, its repositories are left for a later run.
- **Not fetched:** X posts (their date comes from the post ID), addresses on
  a private network, anything that is not http or https.
- **New script "Remove publication dates from one source"**, to take back the
  dates of a source that proves unreliable, such as the unverified Date column.
- **Not verified:** the lookup against real sites. The development session
  cannot reach the web from code, so it was tested with a local server and
  stand-in pages. YouTube and Hugging Face pages may declare nothing usable.
