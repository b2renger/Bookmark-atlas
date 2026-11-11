"""Clustering engines and the measures used to compare them.

Two engines behind one function, ``fit``:

- ``e-means``: the reference implementation from Faiss, vendored in
  ``atlas.vendor.e_means``. With ``init="k-means++"`` it starts from
  k-means++ centres instead of random points.
- ``k-means``: Lloyd's algorithm with k-means++ initialisation, the classic
  baseline, written here with NumPy.
"""

from __future__ import annotations

import contextlib
import io
import time

from atlas.analysis import require_numpy

ALGORITHMS = ("e-means", "e-means++", "k-means")


def squared_distances(np, x, centroids):
    d = (
        np.einsum("ij,ij->i", x, x)[:, None]
        - 2.0 * (x @ centroids.T)
        + np.einsum("ij,ij->i", centroids, centroids)[None, :]
    )
    return np.maximum(d, 0.0)


def assign(np, x, centroids):
    """Nearest centroid of each row, and the mean squared distance to it."""
    d = squared_distances(np, x, centroids)
    labels = d.argmin(axis=1)
    return labels, float(d[np.arange(len(x)), labels].mean())


def kmeans_plus_plus(np, x, k: int, rng):
    """k-means++ seeding: spread the first centres out, proportionally to distance."""
    n = len(x)
    centroids = np.empty((k, x.shape[1]), dtype=np.float32)
    centroids[0] = x[rng.integers(n)]
    closest = squared_distances(np, x, centroids[:1])[:, 0]
    for index in range(1, k):
        total = float(closest.sum())
        if total <= 0:
            pick = rng.integers(n)
        else:
            pick = min(int(np.searchsorted(np.cumsum(closest), rng.random() * total)), n - 1)
        centroids[index] = x[pick]
        closest = np.minimum(closest, squared_distances(np, x, centroids[index : index + 1])[:, 0])
    return centroids


def lloyd(np, x, k: int, seed: int, iterations: int = 25, restarts: int = 1):
    """Lloyd's k-means from k-means++ centres. With ``restarts`` above 1, the
    best of several starts is kept, as most k-means libraries do."""
    best = None
    for attempt in range(max(1, restarts)):
        rng = np.random.default_rng([seed, attempt])
        centroids = kmeans_plus_plus(np, x, k, rng)
        labels = None
        done = 0
        for done in range(1, iterations + 1):  # noqa: B007
            new_labels = squared_distances(np, x, centroids).argmin(axis=1)
            if labels is not None and (new_labels == labels).all():
                break
            labels = new_labels
            for index in range(k):
                members = x[labels == index]
                if len(members):
                    centroids[index] = members.mean(axis=0)
                else:  # an empty cluster restarts on the point furthest from its centre
                    far = squared_distances(np, x, centroids)[np.arange(len(x)), labels].argmax()
                    centroids[index] = x[far]
        loss = assign(np, x, centroids)[1]
        if best is None or loss < best[0]:
            best = (loss, centroids, done)
    return best[1], best[2]


def fit(
    x,
    k: int,
    *,
    algorithm: str = "k-means",
    seed: int = 0,
    epochs: int | None = None,
    restarts: int = 3,
):
    """Cluster the rows of ``x`` into ``k`` groups.

    Returns ``(labels, centroids, metrics)``. Labels are those of the final
    centroids (each row with its nearest one), whatever the engine.
    ``restarts`` applies to k-means only.
    """
    np = require_numpy()
    if algorithm not in ALGORITHMS:
        raise ValueError(f"Unknown algorithm {algorithm!r}. Choose from: {', '.join(ALGORITHMS)}.")
    x = np.ascontiguousarray(x, dtype=np.float32)
    n = len(x)
    if not 2 <= k <= n:
        raise ValueError(f"The number of clusters must be between 2 and {n}.")

    started = time.perf_counter()
    extra = {}
    if algorithm == "k-means":
        centroids, iterations = lloyd(np, x, k, seed, epochs or 25, restarts)
        extra["iterations"] = iterations
        extra["restarts"] = restarts
    else:
        from atlas.vendor.e_means import NumpyEmeans

        model = NumpyEmeans(x.shape[1], k, nepochs=epochs or 5, seed=seed)
        init = None
        if algorithm == "e-means++":
            init = kmeans_plus_plus(np, x, k, np.random.default_rng(seed))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            centroids, _ = model.train(x, init_centroids=init)
        centroids = np.asarray(centroids, dtype=np.float32)
        recipe = model.recipe or {}
        extra["epochs"] = epochs or 5
        extra["batch_size"] = recipe.get("batch_size")
        extra["revived"] = int(model.n_splits)
    seconds = time.perf_counter() - started

    labels, mse = assign(np, x, centroids)
    sizes = np.bincount(labels, minlength=k)
    metrics = {
        "mse": mse,
        "seconds": seconds,
        "empty_clusters": int((sizes == 0).sum()),
        "smallest": int(sizes.min()),
        "largest": int(sizes.max()),
        **extra,
    }
    return labels, centroids, metrics


def adjusted_rand_index(a, b) -> float:
    """Agreement between two clusterings: 1 identical, about 0 unrelated."""
    np = require_numpy()
    a = np.asarray(a)
    b = np.asarray(b)
    n = len(a)
    if n < 2:
        return 1.0
    _, ai = np.unique(a, return_inverse=True)
    _, bi = np.unique(b, return_inverse=True)
    table = np.zeros((ai.max() + 1, bi.max() + 1), dtype=np.int64)
    np.add.at(table, (ai, bi), 1)

    def pairs(values):
        return float((values * (values - 1) // 2).sum())

    together = pairs(table)
    rows, columns = pairs(table.sum(axis=1)), pairs(table.sum(axis=0))
    expected = rows * columns / (n * (n - 1) / 2)
    maximum = (rows + columns) / 2
    return 1.0 if maximum == expected else (together - expected) / (maximum - expected)


def silhouette(x, labels, sample: int = 2000, seed: int = 0) -> float:
    """Mean silhouette: how much closer points are to their own cluster than
    to the next one. From -1 (wrong) through 0 (overlapping) to 1 (separate)."""
    np = require_numpy()
    x = np.asarray(x, dtype=np.float32)
    labels = np.asarray(labels)
    if len(x) > sample:
        keep = np.random.default_rng(seed).choice(len(x), sample, replace=False)
        x, labels = x[keep], labels[keep]
    names, inverse = np.unique(labels, return_inverse=True)
    if len(names) < 2:
        return 0.0
    distances = np.sqrt(squared_distances(np, x, x))
    sizes = np.bincount(inverse).astype(np.float64)
    sums = np.zeros((len(x), len(names)))
    for index in range(len(names)):
        sums[:, index] = distances[:, inverse == index].sum(axis=1)
    own = sums[np.arange(len(x)), inverse]
    own_size = sizes[inverse]
    a = np.where(own_size > 1, own / np.maximum(own_size - 1, 1), 0.0)
    others = sums / sizes[None, :]
    others[np.arange(len(x)), inverse] = np.inf
    b = others.min(axis=1)
    score = np.where(own_size > 1, (b - a) / np.maximum(np.maximum(a, b), 1e-12), 0.0)
    return float(score.mean())
