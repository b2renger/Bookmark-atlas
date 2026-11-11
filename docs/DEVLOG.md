# Development log

Where the project stands, newest entry first. Each entry says what works, what
was decided and what comes next. Commit messages carry the same information in
short form, so `git log` reads as a history of the project's state.

## 2026-11-11: v0.3.0 published; phase 4, the map

**Published:** v0.3.0. Its first attempt was stopped by the test gate: a test
expected exact cluster sizes from plain e-means on the 72-item toy set, which
is exactly what the e-means report says not to expect. Tests that depended on
e-means outcomes, or on a random draw of the suggested k, were rewritten.

**Works**

- `atlas.analysis.project`: exact t-SNE in NumPy (PCA start, so it is
  repeatable), PCA fallback above 3,500 bookmarks, and a trustworthiness
  measure.
- Layouts are stored as runs of kind `projection`; only the latest is kept.
- Map tab: canvas scatter, topic labels with collision avoidance, hover
  tooltip, click for a detail card, focus a topic (by label or by list),
  highlight search matches, Shift+drag area selection, zoom and pan.
- The selection bar is now shared by the list and the map, with "show only
  these in the list".

**Checked on the real collection**

- Trustworthiness 0.989 (scikit-learn's t-SNE: 0.989; PCA: 0.78). My measure
  agrees with scikit-learn's to six decimals.
- The 16 k-means clusters are better separated on the map than in the vectors
  (silhouette 0.39 against 0.25), so topics appear as islands.
- Driven in Chrome, light and dark: draw the map, focus a topic, highlight
  "blender" (29 matches), hover, open a detail, select an area (67
  bookmarks), show them in the list, tag them. No page errors.

**Decided**

- Colour encodes state, not cluster identity. A scatter plot compares every
  pair of colours, and the palette checker passes only three slots under that
  rule, in both light and dark. Topics get text labels and a "Focus on" list.
- Labels that would overlap a bigger topic's label are not drawn; every topic
  stays reachable through the list.
- One scale per axis to use the whole canvas. Distances on a t-SNE map are
  only meaningful locally anyway, and the note under the map says "close
  together", not "far apart".
- The selection bar is sticky over the list and static above the map.

**Found by the browser run**

- The sticky selection bar covered the map's tools. Fixed.
- The server printed a traceback when a page closed mid-answer. Silenced.

**Known gaps**

- No touch gestures for pinch zoom. No filter by tag, platform or date on the
  map.
- The map is not redrawn automatically when bookmarks are added: the note
  says how many are missing and the button redraws.

**Next**

- Publish 0.4.0. Phase 5: saved views and snapshots.

## 2026-11-11: phase 3, vectors, clusters and the e-means test

**Published before this:** v0.2.1 (portable data folder), with both smoke
tests green.

**Works**

- `atlas embed`, `atlas cluster [--k N|auto] [--algo ...]`, `atlas compare`.
- Schema version 2: `embeddings`, `runs`, `run_items`, `run_artifacts`,
  `cluster_edits`.
- Clusters tab: run, list of runs, cluster cards, rename, delete run, "show
  bookmarks" (a cluster filter on the list that works with search, selection
  and export).
- e-means vendored from Faiss in `src/atlas/vendor/e_means` (MIT, commit
  f777f7d), with a NumPy replacement for `faiss.knn`.

**Checked on the real collection (956 bookmarks, groomed)**

- Full comparison in `docs/reports/e-means-test.md`: e-means equals k-means in
  quality at this size.
- Auto picks 16 clusters; they read as topics.
- Clusters tab driven in Chrome: auto run, e-means run with 24, rename, filter
  the list by cluster, select the whole cluster, delete a run. No page errors.

**Decided**

- k-means is the default engine. e-means and e-means++ are selectable.
- Vectors are LSA (TF-IDF then truncated SVD), 48 directions stored, the first
  16 used for clustering. Chosen from measurements, not taste: see the report.
- Vectors are rebuilt whenever a bookmark's text changed; a run stores its
  labels and centroids, so an old run stays viewable.
- NumPy is the only new dependency. It is optional for the Python package
  (`pip install .[analysis]`) and bundled in the installer.
- The k-means baseline keeps the best of 3 starts, as libraries do.

**Found along the way**

- Plain e-means with random starts merges topics on very small sets (15 of 20
  seeds correct on a 72-item toy, against 20 of 20 for k-means), and its
  scaling laws pick odd settings far below the sizes the paper studied.
- With one start, my k-means missed the optimum on a toy set; restarts fixed
  it and scikit-learn now agrees to 0.1%.

**Known gaps**

- LSA vectors only. No neural embedding model yet.
- Vocabulary is capped at 6,000 terms and the matrix is dense: fine for
  thousands of bookmarks, not designed for hundreds of thousands.
- No 2D map yet: that is phase 4.

**Next**

- Publish 0.3.0. Phase 4: the map.

## 2026-11-11: v0.2.0 published; portable data folder (0.2.1)

**Published**

- v0.2.0 is on GitHub Releases with the Windows installer and `latest.yml`
  (run 43102183630: tests, build, smoke test, publish, all green).
- Releases are now cut from the Actions tab: run `release` with a version. The
  workflow creates the tag. (Tags could not be pushed from the development
  environment.)
- CI is green on Linux and Windows.

**Works (0.2.1)**

- All user data is in one folder: `atlas.sqlite`, `scripts/`, `backups/`,
  `secrets.json` (the Notion token).
- **Atlas > Choose data folder…**: pick a folder. If it already holds a
  collection, the app switches to it. Otherwise it offers to copy the current
  data there or to start empty. The app restarts on the new folder.
- Which folder is in use is remembered per machine, in `location.json` in the
  app's own settings folder. `ATLAS_DATA_DIR` overrides it.
- If the chosen folder is missing at start, the app asks: choose another, use
  the default, or quit. It never silently starts an empty collection.
- Settings shows the folder and explains how to move it.

**Verified how**

- The copy and pointer logic is plain Node (`desktop/datafolder.js`) with nine
  unit tests: copy is complete, the source is untouched, an existing
  collection is never overwritten, a folder inside the current one is refused,
  an open database is refused, a damaged pointer is ignored.
- The release workflow starts the packaged app a second time with a chosen
  folder (with a space in its name) and checks that the backend reports that
  folder, and that the database is created there and not in the settings
  folder.
- Not covered by any automated check: the dialogs themselves (the menu flow,
  the restart). They need a person on Windows.

**Decided**

- "Move" is a copy that leaves the original in place. The pointer is written
  last, so an interrupted copy changes nothing.
- The token stays inside the data folder, so that copying the folder really
  brings everything back. The cost: the folder must not be shared. The
  interface says so, and sharing is done with bundles.
- `backend.log` and Electron's own files stay in the per-machine settings
  folder; they are not user data.
- The backend is stopped, and its exit awaited, before files are copied.

**Known gaps**

- Two computers on the same synced folder at the same time: sync tools can
  produce conflict copies of the database. One machine at a time.

**Next**

- Publish 0.2.1. With 0.2.0 installed first, this is also the first real test
  of self-update.
- Phase 3: embeddings and the e-means test. The reference e-means code has
  been read (Faiss `contrib/e_means`, commit f777f7d): its NumPy class needs
  only `faiss.knn`, so it can run with NumPy alone.

## 2026-11-11: batch edits, sharing, first green build

**Works**

- Bookmarks tab: a checkbox per row, "select all N matching", then add a tag,
  remove a tag, delete, or export the selection. Each batch is one journaled
  run with an Undo link, and appears in the Grooming history.
- Bundles: `Export selection` downloads a `.atlas.json` file. Settings >
  Import a file accepts it (as well as CSV). Command line:
  `atlas export --format bundle [--search TEXT]`, `atlas import-bundle FILE`.
- Release notes are read from `docs/releases/<tag>.md`; phase summaries are in
  `docs/PHASES.md`.

**The build works**

- The trial run of the release workflow succeeded on GitHub (run 43098751858):
  tests, Windows installer (138 MB), and the smoke test of the packaged app,
  which reported `0 bookmarks · 0 tags · v0.0.0-dev.2` from the real page.
- In that run the bundled Python reached the real Notion API and got the
  expected 401 for a made-up token: network, TLS and certificates work in the
  shipped runtime. This is the first real contact with Notion.
- The earlier "startup failure" and the long queue were on GitHub's side.

**Found by CI on Windows**

- Windows has no time zone database Python can read, so a Notion date naming a
  zone lost its offset. The installer now bundles the `tzdata` package and the
  build checks that a named zone resolves.

**Checked in Chrome on the real export**

- Search "gaussian splatting", select all 121 matching, add a tag (121
  changed), undo (0 left), remove a tag, export (121 bookmarks, 80 kB), delete
  one with the two-click confirm, undo. A second, empty Atlas imported the
  exported file: 121 bookmarks created.
- That run found a bug, now fixed: after a delete the result line and its Undo
  link vanished with the selection.

**Decided**

- A bundle is JSON with curated fields only. It does not carry the sender's
  save dates: for the receiver a shared bookmark is saved at import time
  (source `shared_import`). Publication dates travel.
- A shared link the receiver already has is kept aside like any duplicate;
  `merge-duplicates` folds its tags in. It never changes the receiver's title,
  summary or dates.
- Hand edits use the grooming engine, so there is one journal and one undo.
- Package version follows the release: 0.2.0.

**Known gaps**

- The installer is 138 MB. The Python part can shrink (pip and unused modules
  are still inside).
- Selection is by tick or by search. There is no filter by tag, platform or
  date yet; that comes with the views.

**Next**

- Tag `v0.2.0`. Then phase 3: embeddings and the e-means test.

## 2026-11-11: phase 2, grooming

**Works**

- Grooming engine: preview, select groups, apply with backup and journal, undo.
- Five built-in scripts: `merge-duplicates`, `normalize-tags`,
  `derive-platform`, `published-from-x-id`, `split-dates`.
- User scripts: any `.py` file in the `scripts` folder beside the database,
  same contract, same safety net. Documented in `docs/GROOMING.md`.
- Command line: `atlas groom list|preview|apply|undo|history`.
- Interface: a Grooming tab with the scripts, their parameters, the preview
  with a checkbox per group, Apply, and a history with Undo. The bookmark list
  now shows platform and publication date.

**Checked on the real 962-row export**

| Script | Result |
| --- | --- |
| merge-duplicates | 6 held rows folded in, 20 changes |
| normalize-tags | 66 spellings merged into 63 tags; 2,368 tags become 2,310 |
| derive-platform | all 956 named: X 489, GitHub 182, Website 179, GitHub Pages 38, Hugging Face 33, YouTube 27, others 8 |
| published-from-x-id | all 489 X posts dated |
| split-dates | 133 date-only values stored as publication dates |

- Undoing all five runs returned the database to exactly its starting state
  (compared by hash over bookmarks, tags, aliases and pending flags).
- Running every script a second time found nothing to do.
- Of the 306 X posts whose Date has no time of day, 304 fall on the same UTC
  day as the time encoded in the post ID. That settles what a date-only value
  means on X rows: the publication day.
- The Grooming tab was driven in Chrome on the same data: preview, untick two
  tag groups, apply, change a parameter, undo. No page errors.

**Decided**

- Scripts only propose. The engine does all writing, so preview, backup,
  journal and undo work the same for built-in and user scripts.
- A proposal has a fingerprint. Apply recomputes it and refuses if it differs
  from the previewed one, so what is written is what was shown.
- Changes are accepted or rejected per group, not per line.
- Every write is conditional on the old value; one mismatch aborts the run.
- A merged tag becomes an alias. Imports follow aliases, so a merged spelling
  does not come back.
- `merge-duplicates` tells a duplicate row from an edited row by this rule: a
  pending record is a duplicate only if the bookmark has another record from
  the same source.
- Backups go to `backups/` beside the database; the ten most recent are kept.
- After grooming, 438 bookmarks still have no save date. Those are the
  date-only rows; the save date will come from Notion's page creation time at
  the first Notion sync.

**Caveats**

- Publication dates taken from the Date column on non-X rows were written by
  the tagging tool and are not verified. 63 of the 133 say 2024, and a few are
  implausible (a 2025-era plugin dated 2018). Their source is recorded as
  `date_column`, so a later script that asks GitHub, Hugging Face and YouTube
  can replace exactly those.
- `normalize-tags` does not merge synonyms ("3DGS", "Gaussian Splatting",
  "3D Gaussian Splatting"). That needs a review-first script.
- Not built yet from the plan's list: `dedupe-similar`, `merge-tag-synonyms`,
  `fetch-published-dates`, `check-links`, and a review screen for rows edited
  in Notion.

**Next**

- Get a build through GitHub Actions and tag `v0.1.0`.
- Phase 3: embeddings and the e-means test.

## 2026-11-11: desktop app, installer and self-update

**Works (written, verified only as far as stated below)**

- `desktop/`: Electron shell. Starts the bundled Python backend, waits for its
  `ATLAS_READY` line, shows the page in a sandboxed window, opens bookmark
  links in the user's browser, stops the backend on quit.
- Self-update with electron-updater from GitHub Releases: check at start,
  background download, "Restart now / Later", install on quit. Menu entry to
  check on demand.
- `desktop/scripts/prepare-backend.mjs`: downloads a standalone Python 3.12
  (python-build-standalone), prunes it, copies the `atlas` package beside it.
- `.github/workflows/release.yml`: tag `vX.Y.Z` builds and publishes the
  Windows installer; a manual run builds without publishing.
- `.github/workflows/ci.yml`: lint and tests on Linux and Windows.
- Settings can import a CSV file, so no command line is needed for the first
  data. Checked in Chrome with the real 962-row export: 956 created, 6 held.
- `atlas doctor`: reports versions, paths, token presence, and whether Notion
  is reachable (a made-up token answered with 401 proves network, TLS and
  certificates).

**Verified how**

- The Electron code could not be run in the development environment (no
  package installs there). It is checked by CI instead: the release workflow
  starts the packaged app with `--smoke-test`, which waits for the page header
  to show the bookmark count and the app version, both of which only appear
  after the page has reached the backend API. See the run results in the
  Actions tab for whether this currently passes.
- The same workflow runs `atlas doctor` with the bundled Python, which is the
  first contact between this code and the real Notion API.
- Not verified by anything: the update path itself (an installed older version
  finding, downloading and installing a newer one). It needs two releases and
  a real Windows machine.

**Decided**

- Electron instead of Tauri, to reuse the unsigned-installer and self-update
  setup of ComfyQ and LlmOnLan: NSIS one-click per-user install,
  `releaseType: release`, artifact names without spaces, `--publish never`
  then `gh release upload` onto a release created first.
- Windows only for now. macOS needs the ad-hoc signing step those repositories
  use; Linux needs an AppImage target. Both are additions to the same files.
- The app version comes from the git tag (`extraMetadata.version`), so a
  release is a tag and nothing else. The shell passes it to the backend, which
  shows it in the page header.
- Python is bundled in the installer because the backend has no dependencies
  yet. When embeddings and clustering bring large libraries, they should be a
  separate download, as LlmOnLan does.
- The backend runs in Python's isolated mode, so a Python installed on the
  user's machine cannot interfere.
- Data lives in the app's user data folder: `atlas.sqlite`, `secrets.json`
  (the Notion token) and `backend.log`. Uninstalling keeps it.
- No `package-lock.json` yet: it could not be generated in the development
  environment. CI uses `npm install` with the version ranges LlmOnLan uses.
  Committing a lockfile produced by CI would make builds reproducible.

**Known gaps**

- On Windows the token file is protected by the user profile's folder
  permissions only. Electron's `safeStorage` would encrypt it.
- No macOS or Linux build.
- The update path is untested (see above).

**Next**

- Tag `v0.1.0`, install it on Windows, import the CSV, try the Notion token.
- Phase 2 grooming, tested on the real extract.

## 2026-11-11: first usable build, local interface

**Works**

- `atlas ui` starts a local web page and opens it in the browser.
- Settings: token field (checked with Notion before it is stored), list of
  tables, column mapping with a guess, Preview and Import, report.
- Bookmarks: list with search over titles, summaries, links and tags.
- The page reopens on the selected table and mapping after a reload.

**Tested how**

- Server and API: automated tests (`tests/test_server.py`).
- The page itself: driven in Chrome through the whole flow against the fake
  Notion: wrong token, right token, list, pick, bad mapping, preview, import,
  second import, list, show more, search, reload. Light and dark, wide and
  420 px wide. This browser run is not part of the test suite yet.
- Bookmark text containing HTML is shown as text, and a `javascript:` link is
  refused at import.
- Still not run against the real Notion API.

**Decided**

- The first build is a local web interface served by the Python package, not
  the Tauri shell. It needs only Python to run, and the same page will be
  wrapped by the desktop shell later. Tauri moves from "phase 4 prerequisite"
  to "packaging step".
- The page calls a JSON API under `/api`; each call lands in the same functions
  as the command line.
- The server listens on 127.0.0.1 only, refuses requests addressed to any
  other host name, and requires a random per-run token on every API call, so
  other web pages open in the browser cannot reach it. A content security
  policy forbids loading anything from elsewhere.
- The API never sends the Notion token back, only a masked hint.
- `atlas ui` creates the database if it does not exist. Other commands still
  refuse a missing database.
- Help text names both token kinds Notion offers: personal access token (no
  sharing step) and internal connection token (database must be shared).

**Known gaps**

- No review screen yet for records waiting for review. `atlas status` and the
  header show the count.
- No CSV import from the interface, only from the command line.
- Imports run inside the request. Fine for a thousand rows; a progress display
  is needed before much larger tables.
- No continuous integration.

**Next**

- Run Preview and Import against the real table and compare with the CSV
  profile (956 bookmarks expected).
- Phase 2 grooming.

## 2026-11-11: phase 1, Notion connection and sync

**Works**

- `atlas notion token`: asks for the token, checks it with Notion, stores it.
- `atlas notion tables`: lists the tables shared with the connection.
- `atlas notion select <id>`: chooses the table and guesses which column feeds
  which field (overridable with `--column`).
- `atlas sync [--full] [--dry-run]`: imports rows. The first run reads the
  whole table; later runs ask only for rows edited since the last one.

**Tested how**

- Against an in-memory fake of the Notion API (`tests/fake_notion.py`) whose
  answers follow the reference documentation read on 2026-11-11.
- **Not yet run against the real Notion API.** No token was available. The
  first real run is the actual test, and `--dry-run` exists for it.

**Decided**

- The token never goes into the database. Lookup order: `NOTION_TOKEN`
  environment variable, system keychain if the optional `keyring` package is
  installed, otherwise `secrets.json` in the user's configuration folder,
  created readable by the owner only.
- A token is checked with Notion before it is stored.
- Schema v1 was edited in place: `source_records.raw_hash` became
  `content_hash`, a hash of the mapped fields (URL, title, summary, sorted
  tags, parsed date). No database existed outside development, so no migration
  was written. From now on schema changes are new migration files.
- The content hash is what makes CSV-then-Notion work: a Notion page whose
  content equals a row already imported from CSV is linked quietly, with
  nothing to review.
- Rows are matched by Notion page ID first, then by normalised URL.
- A row edited in Notion updates its source record and waits for review. The
  bookmark is not touched.
- For Notion rows, `saved_at` is the page creation time (source
  `notion_created`). An empty `saved_at` on an existing bookmark is filled the
  same way and journaled in `changes`; a filled one is never replaced.
- The sync report says on how many rows the page creation time matches the
  Date column. That number tells whether creation time can be trusted as the
  save time (see "Not yet verified" in the plan).
- Rows in Notion's trash are ignored for now. Detecting removals is left for a
  later `--full` pass.
- If Notion says a listing is incomplete, the sync fails and writes nothing.
- Requests are spaced 0.35 s apart; 429, 503 and 529 answers are retried after
  the `Retry-After` delay, up to five attempts.

**Next**

- A first interface: Settings with the token field and table picker.

## 2026-11-11: phase 1, export and status

**Works**

- `atlas export --format csv|json [-o FILE] [--include-deleted]`.
- `atlas status`: counts of bookmarks, deleted, source records, records waiting
  for review, tags, and fields still empty.
- README explains how to run everything so far.

**Checked on the real export**

- CSV and JSON exports both hold 956 bookmarks, in the same order, with the
  same tags.
- Status after import: 6 records waiting for review, 1,843 of 2,368 tags used
  only once, 439 bookmarks without a save date, none with a publication date or
  platform yet. These are the numbers phase 2 should move.

**Decided**

- Exports contain curated fields only: no source records, no journal.
- In CSV, tags share one column joined by ", ", as Notion does. In JSON they
  are a list.
- Deleted and merged bookmarks are left out unless `--include-deleted` is given.

**Not done in phase 1 yet**

- Notion sync (`atlas connect`, `atlas sync`). It needs a Notion connection
  token and the table shared with it, and should be tested against the real
  API, not only against fakes.

**Next**

- Either the Notion sync, or phase 2 grooming on the CSV-imported data. The
  two are independent.

## 2026-11-11: phase 1, CSV import

**Works**

- `atlas import-csv <file>` with `--dry-run` and `--column field=header`.
- Rows are matched to bookmarks by normalised URL. New links become bookmarks;
  links already known are stored as source records and never overwrite
  anything.
- Importing the same file twice writes nothing.
- A bookmark deleted locally stays deleted when its link is imported again.
- A whole file is one transaction: it all succeeds or nothing is written.

**Checked on the real export (962 rows)**

- 956 bookmarks created, 6 rows recognised as duplicates and held for review.
  Three of them have identical URLs. The other three were only found through
  normalisation: a tracking parameter, a YouTube share parameter, and the same
  X post under two account paths. All six are true duplicates.
- 439 dates have no time of day and were left empty on purpose.
- A second import reported 962 rows already imported and created nothing.

**Decided**

- Exact duplicates collapse at import, because the normalised URL is the only
  key a CSV row has. The first row creates the bookmark; later rows for the
  same link are kept as pending source records. Their tags are not merged in
  automatically.
- A date with a time of day goes to `saved_at` (source `date_column`). A date
  without one is ambiguous (save day or publication day), so both date fields
  stay empty and the original value remains in the source record for the
  `split-dates` script.
- `platform` is not filled at import. It is the job of `derive-platform`.
- Tags are stored exactly as written. Normalising them is grooming.
- URL rules so far: scheme, `www.`, fragment, trailing slash and tracking
  parameters are ignored; twitter.com is x.com; an X post is identified by its
  ID alone; a YouTube video by its `v` parameter; GitHub owner and repository
  names are case-insensitive.

**Next**

- `atlas export` and `atlas status`, then the Notion sync.

## 2026-11-11: phase 0, scaffolding

**Works**

- Python package `atlas` under `src/`, standard library only.
- `atlas init` creates the SQLite file and applies migrations.
- Schema version 1: bookmarks, source records, tags, sync state, grooming
  journal, meta.

**Decided**

- Migrations are numbered SQL files in `src/atlas/migrations`. The schema
  version lives in `meta.schema_version`. Commands refuse to run on an outdated
  database and say to run `atlas init`.
- Opening a database that does not exist is an error, so a typo in `--db`
  cannot silently start an empty collection.
- Analysis and saved-view tables are not created yet. They arrive as their own
  migrations with the phases that use them.
- `source_records` gained a `raw_hash` column compared with the first plan, to
  recognise an unchanged record on re-import.
- No continuous integration yet. Tests run locally with `pytest`.

**Next**

- Phase 1: CSV import, then export and status.
