# Bookmark Atlas

A desktop app to explore and refine a bookmark collection: clustering, maps,
nested collections and trends, built to encourage discovery.

Bookmarks (with summaries and tags) already live in a Notion table. This app
pulls them into a local SQLite file and works from there.

It is also a test bed for **e-means**, a clustering algorithm published in
November 2026 ([arXiv 2611.09501](https://arxiv.org/abs/2611.09501)).

## Status

Planning. No code yet. See [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## Planned shape

- **Extractor**: incremental import from Notion through its API.
- **Local store**: one SQLite file, exportable to CSV and JSON.
- **Analysis**: embeddings, e-means (with k-means as a baseline), 2D projection,
  self-organising map.
- **Views**: map, SOM grid, pearl tree, link graph, tendencies over time.
- **Snapshots**: saved runs and views, exportable as a single shareable file.
