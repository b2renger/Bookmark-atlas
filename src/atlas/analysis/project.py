"""Laying the collection out in two dimensions for the map.

Uses t-SNE, which places bookmarks with similar vectors next to each other.
It is written here with NumPy (exact version, all pairs), which is fine for a
few thousand bookmarks. Larger collections fall back to the two main
directions of the vectors (PCA): faster, cruder.
"""

from __future__ import annotations

from atlas.analysis import require_numpy

TSNE_LIMIT = 3500
LANDMARKS = 2500  # bookmarks laid out exactly when the collection is larger than the limit
TRUST_SAMPLE = 3000


def pca(np, x, dims: int = 2):
    centred = x - x.mean(axis=0, keepdims=True)
    u, s, _ = np.linalg.svd(centred, full_matrices=False)
    return u[:, :dims] * s[:dims]


def _affinities(np, x, perplexity: float):
    """Symmetric neighbour probabilities, each point's spread set by bisection
    so that it has about ``perplexity`` effective neighbours."""
    n = len(x)
    squared = np.einsum("ij,ij->i", x, x)
    distances = np.maximum(squared[:, None] - 2.0 * (x @ x.T) + squared[None, :], 0.0)
    target = np.log(perplexity)
    beta = np.ones(n)
    low = np.zeros(n)
    high = np.full(n, np.inf)
    p = np.zeros((n, n))
    for _ in range(60):
        p = np.exp(-distances * beta[:, None])
        np.fill_diagonal(p, 0.0)
        total = np.maximum(p.sum(axis=1), 1e-300)
        entropy = np.log(total) + beta * (distances * p).sum(axis=1) / total
        p /= total[:, None]
        too_wide = entropy > target
        done = np.abs(entropy - target) < 1e-5
        if done.all():
            break
        low = np.where(too_wide & ~done, beta, low)
        high = np.where(~too_wide & ~done, beta, high)
        beta = np.where(done, beta, np.where(np.isinf(high), beta * 2.0, (low + high) / 2.0))
    p = (p + p.T) / (2.0 * n)
    return np.maximum(p, 1e-12)


def tsne(x, *, seed: int = 0, perplexity: float = 30.0, iterations: int = 500):
    """Exact t-SNE. Starts from the PCA layout, so the same data and seed give
    the same map, and maps of slightly different data resemble each other."""
    np = require_numpy()
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    if n < 5:
        return pca(np, x) if n > 2 else np.zeros((n, 2))
    perplexity = min(perplexity, max(2.0, (n - 1) / 3.0))
    p = _affinities(np, x, perplexity)

    y = pca(np, x)
    y = y / max(float(np.abs(y).max()), 1e-12) * 1e-4
    y += np.random.default_rng(seed).normal(scale=1e-6, size=y.shape)  # break exact ties
    velocity = np.zeros_like(y)
    gains = np.ones_like(y)
    exaggeration = 12.0
    for step in range(iterations):
        squared = np.einsum("ij,ij->i", y, y)
        num = 1.0 / (1.0 + np.maximum(squared[:, None] - 2.0 * (y @ y.T) + squared[None, :], 0.0))
        np.fill_diagonal(num, 0.0)
        q = np.maximum(num / num.sum(), 1e-12)
        weight = ((p * (exaggeration if step < 100 else 1.0)) - q) * num
        gradient = 4.0 * (weight.sum(axis=1)[:, None] * y - weight @ y)
        same_direction = np.sign(gradient) == np.sign(velocity)
        gains = np.maximum(np.where(same_direction, gains * 0.8, gains + 0.2), 0.01)
        momentum = 0.5 if step < 250 else 0.8
        velocity = momentum * velocity - 200.0 * gains * gradient
        y = y + velocity
        y -= y.mean(axis=0, keepdims=True)
    return y


def _by_landmarks(np, x, seed: int):
    """A map for a collection too large for exact t-SNE, whose cost grows with
    the square of the size. A sample of ``LANDMARKS`` bookmarks is laid out
    exactly; every other bookmark is then set among the sample's bookmarks it
    resembles most, nearer to the closest ones."""
    n = len(x)
    rng = np.random.default_rng(seed)
    count = min(LANDMARKS, TSNE_LIMIT)
    chosen = np.sort(rng.choice(n, size=count, replace=False))
    placed = tsne(x[chosen], seed=seed)
    y = np.empty((n, 2))
    y[chosen] = placed
    marks = x[chosen]
    marks_squared = np.einsum("ij,ij->i", marks, marks)
    # How far apart neighbours are drawn, to spread bookmarks that land together.
    spread = float(np.median(np.abs(placed - placed.mean(axis=0)))) / np.sqrt(count) * 1.5
    is_mark = np.zeros(n, dtype=bool)
    is_mark[chosen] = True
    rest = np.flatnonzero(~is_mark)
    near = min(6, count - 1)
    for start in range(0, len(rest), 1024):
        rows = rest[start : start + 1024]
        block = x[rows]
        distance = np.maximum(
            np.einsum("ij,ij->i", block, block)[:, None] - 2.0 * (block @ marks.T) + marks_squared,
            0.0,
        )
        closest = np.argpartition(distance, near, axis=1)[:, :near]
        d = np.take_along_axis(distance, closest, axis=1)
        weight = 1.0 / (d + 1e-9) ** 2  # the nearest one counts most
        weight /= weight.sum(axis=1, keepdims=True)
        y[rows] = np.einsum("ij,ijk->ik", weight, placed[closest])
    y[rest] += rng.normal(scale=spread, size=(len(rest), 2))
    return y


def layout(x, *, seed: int = 0):
    """``(points, method)``: an (n, 2) array scaled into the unit square."""
    np = require_numpy()
    x = np.asarray(x, dtype=np.float64)
    if len(x) <= TSNE_LIMIT:
        method, y = "t-sne", tsne(x, seed=seed)
    else:
        method, y = "t-sne-landmarks", _by_landmarks(np, x, seed)
    if len(y):
        low, high = y.min(axis=0), y.max(axis=0)
        span = float(max((high - low).max(), 1e-12))
        # One scale for both axes, so distances are not distorted; centred.
        y = (y - (low + high) / 2.0) / span + 0.5
    return y, method


def trustworthiness(x, y, neighbours: int = 10) -> float:
    """How far the map can be trusted, from 0 to 1: whether points drawn as
    neighbours were neighbours in the original vectors."""
    np = require_numpy()
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    n = len(x)
    if n <= neighbours * 2:
        return 1.0
    if n > TRUST_SAMPLE:
        # Measured among a sample: the full measure needs every pair of bookmarks.
        keep = np.sort(np.random.default_rng(0).choice(n, size=TRUST_SAMPLE, replace=False))
        x, y, n = x[keep], y[keep], TRUST_SAMPLE

    def squared(a):
        s = np.einsum("ij,ij->i", a, a)
        d = s[:, None] - 2.0 * (a @ a.T) + s[None, :]
        np.fill_diagonal(d, np.inf)
        return d

    order_x = np.argsort(squared(x), axis=1)
    rank_x = np.empty((n, n), dtype=np.int64)
    rank_x[np.arange(n)[:, None], order_x] = np.arange(n)[None, :]
    near_y = np.argsort(squared(y), axis=1)[:, :neighbours]
    ranks = rank_x[np.arange(n)[:, None], near_y] - neighbours + 1
    penalty = float(ranks[ranks > 0].sum())
    return 1.0 - penalty * 2.0 / (n * neighbours * (2.0 * n - 3.0 * neighbours - 1.0))
