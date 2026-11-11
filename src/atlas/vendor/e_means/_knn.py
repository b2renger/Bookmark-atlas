"""Nearest-centroid search for the vendored e-means.

The reference code calls ``faiss.knn(x, centroids, 1)`` and nothing else from
Faiss. This module provides that one function with NumPy, so e-means runs
without the Faiss library. The search is exact, as Faiss's is, so results are
the same up to floating-point rounding. If Faiss is installed it is used.

Written for Bookmark Atlas; not part of Faiss.
"""

import numpy as np

METRIC_L2 = 1
METRIC_INNER_PRODUCT = 0

try:  # pragma: no cover - only when Faiss happens to be installed
    import faiss as _faiss
except ImportError:
    _faiss = None

_CHUNK = 16384


def knn(x, centroids, k=1, metric=METRIC_L2):
    """Distances and indices of the ``k`` nearest centroids of each row of ``x``.

    Returns ``(distances, labels)`` shaped ``(n, k)``: squared L2 distances
    (or inner products), float32, and int64 indices. Only ``k == 1`` is
    needed and supported here.
    """
    if _faiss is not None:
        if metric == METRIC_INNER_PRODUCT:
            return _faiss.knn(x, centroids, k, metric=_faiss.METRIC_INNER_PRODUCT)
        return _faiss.knn(x, centroids, k)
    if k != 1:
        raise NotImplementedError("the NumPy stand-in only finds the single nearest centroid")
    x = np.ascontiguousarray(x, dtype=np.float32)
    centroids = np.ascontiguousarray(centroids, dtype=np.float32)
    n = x.shape[0]
    values = np.empty((n, 1), dtype=np.float32)
    labels = np.empty((n, 1), dtype=np.int64)
    centroid_norms = np.einsum("ij,ij->i", centroids, centroids)
    for start in range(0, n, _CHUNK):
        block = x[start : start + _CHUNK]
        products = block @ centroids.T
        if metric == METRIC_INNER_PRODUCT:
            best = products.argmax(axis=1)
            score = products[np.arange(block.shape[0]), best]
        else:
            distances = np.einsum("ij,ij->i", block, block)[:, None] - 2.0 * products
            distances += centroid_norms[None, :]
            best = distances.argmin(axis=1)
            score = np.maximum(distances[np.arange(block.shape[0]), best], 0.0)
        values[start : start + _CHUNK, 0] = score
        labels[start : start + _CHUNK, 0] = best
    return values, labels
