# Phase summaries

One short summary per phase, written when the phase is published. Details are
in [DEVLOG.md](DEVLOG.md); the road ahead is in
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

| Phase | Subject | Release | State |
| --- | --- | --- | --- |
| 0 | Scaffolding | v0.2.0 | done |
| 1 | Local store and import | v0.2.0 | done; the Notion sync was confirmed with a real token on 2026-11-12 |
| 2 | Grooming, batch edits, sharing | v0.2.0, v0.2.1 | done, four planned scripts still to write |
| 3 | Vectors, clusters and the e-means test | v0.3.0 | done |
| 4 | Map view | v0.4.0 | done |
| 5 | Saved views and snapshots | v0.5.0 | done |
| 6 | Self-organising map and pearl tree | v0.6.0 | done, drag and drop, merge and split left out |
| 7 | Links and tendencies | v0.7.1 | done (v0.7.0 was built from the wrong commit) |
| 8 | Shared filter, animated layouts, timeline, 3D | v0.8.0 | done for list, map and grid |
| 9 | Publication dates read from the pages | v0.9.0 | done; not yet run against real sites |
| 10 | Filter everywhere; Threads and the whole-tree drawing | v0.10.0 | done |
| 11 | Grooming loose ends | v0.11.0 | done; online parts not run against real sites |
| 12 | Visual loose ends, 3D link graph, pictures to keep | v0.12.0 | done |
| 13 | Snapshot comparison, Linux and macOS builds, screenshots | v0.13.0 | done; Linux and macOS not yet used by a person |
| 14 | Workspaces and browser bookmarks | v0.14.0 | done |
| 15 | Pan and zoom everywhere; the bookmark side panel | v0.15.0 | done |
| 16 | In-app guide, getting-started steps, Notion token help | v0.16.0 | done |
| 17 | Grid cells named apart; 3D arrangements in plain sight | v0.17.0 | done |

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

## Phase 10: the filter everywhere, Threads, and the whole tree

- **The filter bar now also applies to Tree, Links and Trends.** Trends are
  recomputed for the bookmarks that pass. In Links, the threads and the lists
  follow the filter; the picture of topics keeps showing the whole collection
  and says so.
- **Threads (Links tab).** All bookmarks on one line, by time or by topic.
  An arc joins a bookmark to each of its three closest neighbours: above the
  line when the two are in different topics, below otherwise. By time, a tall
  arc is an idea you came back to after a long while.
- **Reading a thread:** point at a bookmark, click to keep it, read why each
  link exists (shared tags, how far apart in time, how alike), follow one to
  the next bookmark. "Surprise me" picks a link across topics, far apart in
  time when there is one.
- **The whole tree (Tree tab).** Families, topics and parts from the centre
  outwards, one stroke per bookmark on the rim, longer when more recent. Click
  a branch to open it in the pearls, a stroke to select the bookmark.
- **What was taken from the data artists read for this** (Stefanie Posavec,
  Giorgia Lupi, Jer Thorp): an entry point with layers behind it, room to get
  lost, the context kept in view (why two bookmarks are linked; which have no
  date), and no pretence that a resemblance is a fact. The forms themselves
  are standard ones (arc diagram, radial tree), drawn for this data.
- **Colour still means state only.** The pictures get their character from
  line, density and transparency.
- **On the real collection:** 2,100 links, 329 across topics, 123 across
  topics and more than six months apart.
- **Left out:** saved views do not remember a pinned thread; the filter does
  not rebuild the topic picture or the tree.

## Phase 11: grooming loose ends

- **Review what changed in Notion.** Rows edited in Notion after import were
  only kept aside. The new script lists them with what differs; "take" brings
  Notion's title, summary and tags in, "keep" marks them as seen.
- **Merge near-duplicates.** Same thing under two links, found by comparing
  the words of titles and summaries. A guess, to review group by group.
- **Merge tag synonyms.** Plural and singular; initials and the words they
  stand for, when only one tag fits (40 proposals on the real collection).
- **Check the links, then "Tag dead links".** Same two steps as the date
  lookup. Only a "not found" answer or a vanished site counts as gone;
  refusals and timeouts flag nothing. The script adds a tag and deletes nothing.
- **Dates for Hugging Face and YouTube.** Hugging Face models, datasets and
  spaces: the creation day from its public API. YouTube: the date in the
  video's page details.
- **Scripts are listed in the order worth running them** (eleven now).
- **Not verified:** the Hugging Face API field (`createdAt`) and the YouTube
  page format, which could not be reached from the development session.

## Phase 12: visual loose ends, the 3D link graph, pictures to keep

- **Links (3D) on the Map.** A fifth arrangement: every bookmark tied to its
  three closest, floating in space. The shape forms while you watch (bookmarks
  push apart, links pull together), then topic names settle where each topic
  ended up. Drag to turn; point at a bookmark to light its links.
- **Save any picture** (map, threads, tree) as an image, or as a small web
  page that carries the image with its title and the words that explain it.
  The page has no script and needs nothing else to open.
- **Date brush.** A small histogram of bookmarks per month sits in the filter
  bar; drag across it to set From and To, double-click to clear.
- **Links follow the filter fully.** The topic picture and the lists are now
  recomputed among the bookmarks that pass.
- **Tree editing.** Drag a topic onto another to merge them (families too, at
  the top level), split a topic into its parts, or give a topic its own
  family. Each asks first; building the tree again starts over.
- **Saved views remember the thread you were reading** and the 3D arrangement.
- **A filter answer that arrives late is ignored,** so quick changes cannot
  leave a stale count.

## Phase 13: snapshot comparison, Linux and macOS builds, screenshots

- **Compare with a snapshot (Views tab).** Choose a snapshot file: see what it
  has that you lack, what you have that it lacks, and where the same bookmark
  carries different tags. One button adds the missing bookmarks. What you
  deleted earlier is counted apart and stays deleted.
- **Linux build:** one AppImage, self-updating.
- **macOS build (Apple silicon):** a disk image, signed ad hoc only. macOS asks
  for confirmation on first open and the app cannot update itself; its
  "Check for updates" opens the download page.
- **Both are started for real by the build** (backend up, page loaded, API
  reached) before being uploaded, and are built after Windows so a problem
  there cannot hold the Windows release back.
- **README screenshots** come from an invented collection
  (`tools/demo_collection.py`), so the public repository shows no real
  bookmark.
- **Not verified:** a person opening the macOS download (Gatekeeper's
  behaviour with a downloaded, unsigned app) and the AppImage's self-update.

## Phase 14: workspaces and browser bookmarks

- **A workspace is an independent copy** of part of the collection: an
  ordinary Atlas database in `Workspaces/<name>/` inside the data folder, with
  its own tags, topics, map, views, grooming history and Notion table.
- **Start from** nothing, everything that is open, the selection, what passes
  the filter, or a topic. The app switches to the new workspace; a banner says
  where you are and takes you back.
- **Bring data in** from any source the app reads, into whatever is open: a
  CSV, a bundle, a snapshot (Compare), another Notion table (each workspace
  remembers its own), and now **a browser's bookmarks file** (Chrome, Firefox,
  Safari, Edge), whose folders become tags and whose "added" dates become
  save dates.
- **Merging back only adds:** bookmarks the collection lacks, and the tags the
  workspace gave to bookmarks both have (one undoable run). What was deleted
  in the collection stays deleted; deleting in a workspace deletes nothing in
  the collection.
- **Take data out** as before: export a selection, or a snapshot of what is
  open.
- **Workspaces travel with the data folder** (the folder move in the app now
  copies them too).
- **Left out:** renaming a workspace; remembering which one was open when the
  app restarts (it always starts in the main collection); comparing two
  workspaces directly (use Compare with a snapshot of one).

## Phase 15: pan and zoom everywhere; the bookmark side panel

- **Every picture zooms and moves the same way:** scroll to zoom around the
  pointer, drag to move, double-click to go back. Threads, the whole tree, the
  grid, the picture of topics and the monthly bars join the map.
- **Pointing and clicking stay exact while zoomed,** and a drag is never taken
  for a click.
- **One side panel for any bookmark,** from wherever it is named: the list, a
  dot on the map, a thread, a stroke of the tree, a grid cell, the lists in
  Links. It shows the summary, dates, tags (click one to filter on it), the
  bookmarks most like it (click one to go on), and **Open in the browser**.
- **Ctrl, Shift or middle click** on a name still goes straight to the page.
- **Read as:** the panel shows the bookmark's details, not the web page
  itself. Showing the live page inside the app is a separate piece of work
  (many sites refuse to be embedded, and it means loading outside content in
  the app).

## Phase 16: the app explains itself

- **Guide tab.** Fourteen short sections: the idea and a first tour, one
  section per tab (what it shows, how to read it, what to press), your data
  and updates, and the words the app uses. Each ends with a button that opens
  the tab it describes.
- **"?" at the top right** opens the guide at the section for the tab you are
  on.
- **Getting started, on the first screen.** Five steps (bring bookmarks in,
  clean, find topics, draw the map, follow a thread and save a view). Each is
  ticked from what the collection already has, the next one is highlighted,
  and each has a button that goes there. They can be put away and brought
  back from the guide; the choice is kept with the collection.
- **How to get a Notion token,** in Settings: the personal access token (five
  steps), the internal connection (five steps, including sharing the table),
  who can create each depending on the plan, and what to do when it fails.
  Written from Notion's developer documentation as read on 2026-11-13.
- **An empty collection now opens on the steps** instead of jumping to
  Settings.
- **Header:** the bookmark count moved to Settings on windows narrower than
  1500 px, to leave room for twelve tabs.

## Phase 17: grid cells named apart; 3D in plain sight

- **Reported:** many grid cells carried the same name ("Open Source", "LLM").
  Each cell was named by its most frequent telling tag, and neighbours share
  that tag by construction.
- **Now each cell is named by what sets it apart:** tags carried by many cells
  weigh less, and a name given to a larger cell is not given again while the
  cell has another tag that describes it. On the test collection: 88 cells,
  88 different names (before: "Open Source" on 4, "ComfyUI" on 5).
- **Reported:** the 3D views could not be found. They were two entries of a
  drop-down that only appeared once the map was drawn.
- **Now the five arrangements are buttons above the map,** always visible,
  the two 3D ones labelled as such. Pressed before the map exists, they say
  to draw the map first.

## Phase 18: the 3D link graph organises itself by topic

- **Reported, with a screenshot of 3,396 bookmarks:** the link graph settled
  into one flat, even mesh in which no topic could be told from another.
- **Why:** every bookmark pushed every other and every link pulled the same,
  from a start that was the flat map. With three links per bookmark, nearly
  all inside a topic, nothing held topics apart, and nothing gave depth.
- **Now topics are placed first,** as a small graph of their own: topics that
  share links sit close, the others are pushed apart on all three axes. Each
  bookmark starts near its topic and is held to it; links inside a topic pull
  hard, links across topics pull gently and stretch between the clouds.
- **Drawing:** links inside a topic are a faint texture, links across topics
  are stronger; far dots are fainter; dots shrink when there are thousands;
  the picture turns slowly until it is taken hold of (not when the system
  asks for reduced motion). Topic names follow their clouds.
- **Speed:** bookmarks only push those near them, found with a grid of boxes.
  Measured in Node on 3,400 invented bookmarks: 6.6 ms a step against 19.3,
  and 180 steps instead of 300 (1.2 s against 5.8 s of computing). In the
  browser the whole settling, drawing included, takes about 5.5 s for 3,400
  bookmarks and 1.7 s for 956.
- **Measured:** share of bookmarks nearer to their own topic's centre than to
  any other: 13% before, 100% now (3,400 invented); 61% before, 99% now (the
  956-bookmark test copy). Empty space between a topic and its nearest
  neighbour: about 0.9 topic radius on average.

## Phase 19: the page fills the window and makes room for the side panel

- **Reported, with a screenshot of a 1912 px window:** the page stayed a
  1040 px column, and the side panel lay over empty space beside it.
- **Now the page fills the window,** centred, up to 1760 px. The map, the
  threads and the tree grow with the window's height so a picture and its
  controls fit on one screen.
- **Opening the side panel moves the page over** instead of covering it: the
  page is centred in what is left and the pictures are redrawn at their new
  size; closing it gives the room back. On windows narrower than 1100 px the
  panel lies over the page as before.
- **Measured in a browser:** 1912 px window: page 1760 px with equal margins,
  1472 px with the panel open, back to 1760 px on closing; 1366 px: 1366 then
  926; 900 px: unchanged, panel over the page. No sideways scrolling.

## Phase 20: import a database dump, and collections of fifteen thousand

- **Asked for:** read an old site's database export (a phpMyAdmin dump of a
  MariaDB database, 15,252 saved links from 2007 to 2019) into Atlas.
- **`atlas.sqldump`** reads the `.sql` file as text and never runs it. It
  finds the table of links (a column named url or link), its title,
  description and date columns, and the tags tied to it through a table of
  pairs. Everything else in the dump is left where it is.
- **Privacy:** no account, author, e-mail address or password is copied, not
  even into the record kept of what was read. Addresses inside descriptions
  are replaced; a link carrying an address is skipped. A test searches the
  resulting file for them.
- **Repairs on the way in:** accents written in one encoding and read in
  another, HTML entities, quotes stored as backslashes; the same link saved
  several times becomes one bookmark; text that filled its column is marked
  (an ellipsis, or the tag "link cut short"); tags used once are left out.
- **Where it goes:** always a new workspace, opened after the import, from
  Settings (Import a file, with a preview) or `atlas import-sql`.
- **Large collections:** the neighbour search works a block of rows at a time
  (the threads went from 4.8 GB to 0.5 GB of memory on 14,163 bookmarks);
  above 3,500 bookmarks the map lays out a sample of 2,500 exactly and sets
  the others among them (60 s); thousands of dots are drawn a shade at a time
  (84 ms a frame down to 10); the more lines, the fainter each.
