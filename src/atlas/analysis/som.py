"""Self-organising map: the collection laid out on a grid of cells.

Each cell holds a prototype vector. Training pulls prototypes towards the
bookmarks, and neighbouring cells towards each other, so that every bookmark
lands in a cell and neighbouring cells hold related bookmarks. Unlike the
map, the grid shows *density*: where the collection is thick, where it is
thin, and which themes border which.

Batch training, started from the two main directions of the data, so the same
collection always gives the same grid.
"""

from __future__ import annotations

import math

from atlas.analysis import require_numpy


def grid_shape(n: int) -> tuple[int, int]:
    """Rows and columns for ``n`` bookmarks: about a dozen bookmarks per cell,
    a little wider than tall, between 3x3 and 20x16."""
    cells = min(320, max(9, n // 12))
    cols = max(3, min(20, math.ceil(math.sqrt(cells * 1.3))))
    rows = max(3, min(16, math.ceil(cells / cols)))
    return rows, cols


def _grid(np, rows: int, cols: int):
    return np.array([(r, c) for r in range(rows) for c in range(cols)], dtype=np.float64)


def train(x, rows: int, cols: int, *, epochs: int = 40):
    """Prototype vectors, shaped ``(rows * cols, d)``, row by row."""
    np = require_numpy()
    x = np.asarray(x, dtype=np.float64)
    mean = x.mean(axis=0)
    centred = x - mean
    _, s, vt = np.linalg.svd(centred, full_matrices=False)
    spread = s[:2] / math.sqrt(max(len(x) - 1, 1))
    directions = vt[:2] if len(vt) >= 2 else np.vstack([vt[0], np.zeros_like(vt[0])])
    if len(spread) < 2:
        spread = np.array([spread[0] if len(spread) else 1.0, 0.0])
    cells = _grid(np, rows, cols)
    # Spread the starting prototypes over the plane of the two main directions.
    u = (cells[:, 1] / max(cols - 1, 1) - 0.5) * 2.0
    v = (cells[:, 0] / max(rows - 1, 1) - 0.5) * 2.0
    weights = mean + np.outer(u * spread[0], directions[0]) + np.outer(v * spread[1], directions[1])

    grid_distance = ((cells[:, None, :] - cells[None, :, :]) ** 2).sum(-1)
    start, end = max(rows, cols) / 2.0, 0.6
    for epoch in range(epochs):
        sigma = start * (end / start) ** (epoch / max(epochs - 1, 1))
        best = assign(x, weights)
        influence = np.exp(-grid_distance[:, best] / (2.0 * sigma * sigma))  # (cells, n)
        total = influence.sum(axis=1, keepdims=True)
        updated = (influence @ x) / np.maximum(total, 1e-12)
        weights = np.where(total > 1e-9, updated, weights)
    return weights


def assign(x, weights):
    """Index of the best matching cell for each row of ``x``."""
    np = require_numpy()
    x = np.asarray(x, dtype=np.float64)
    d = (
        np.einsum("ij,ij->i", x, x)[:, None]
        - 2.0 * (x @ weights.T)
        + np.einsum("ij,ij->i", weights, weights)[None, :]
    )
    return d.argmin(axis=1)


def errors(x, weights, rows: int, cols: int) -> dict:
    """Two standard quality measures.

    - quantization error: mean distance from a bookmark to its cell's prototype;
    - topographic error: share of bookmarks whose two best cells are not
      neighbours on the grid (0 is a perfectly ordered grid).
    """
    np = require_numpy()
    x = np.asarray(x, dtype=np.float64)
    d = (
        np.einsum("ij,ij->i", x, x)[:, None]
        - 2.0 * (x @ weights.T)
        + np.einsum("ij,ij->i", weights, weights)[None, :]
    )
    order = np.argsort(d, axis=1)[:, :2]
    first, second = order[:, 0], order[:, 1]
    apart = np.maximum(np.abs(first // cols - second // cols), np.abs(first % cols - second % cols))
    return {
        "quantization_error": float(np.sqrt(np.maximum(d[np.arange(len(x)), first], 0)).mean()),
        "topographic_error": float((apart > 1).mean()),
    }
