"""Turning bookmarks into vectors.

The vectors are built from the words of each bookmark (title, summary, tags)
with latent semantic analysis: TF-IDF weights, then a truncated singular value
decomposition. Bookmarks that use related words end up close to each other.

This needs no model download and runs with NumPy alone. It knows nothing
about meaning beyond which words appear together in *this* collection; a
neural sentence-embedding model would do better and can replace it later
behind the same interface (see ``build_vectors``).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from atlas.analysis import require_numpy

MODEL_PREFIX = "lsa"
DEFAULT_DIM = 48
MAX_VOCABULARY = 6000

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#]*(?:[.\-'][a-z0-9+#]+)*")

# Words too common to tell bookmarks apart. English and French, since
# summaries may be in either.
STOPWORDS = frozenset(
    """
    a about above after again all also an and any are as at be because been before being below
    between both but by can could did do does doing down during each few for from further had has
    have having he her here hers him his how i if in into is it its itself just me more most my
    no nor not now of off on once only or other our out over own same she so some such than that
    the their them then there these they this those through to too under until up use used uses
    using very via was we were what when where which while who why will with would you your
    new allows allow allowing provides provide providing designed based various including
    le la les un une des du de d et ou mais donc car ni que qui quoi dont où ce cet cette ces
    il elle ils elles on nous vous je tu en dans sur sous par pour avec sans est sont été être
    a ont avoir plus moins très aussi au aux se sa son ses leur leurs pas ne
    """.split()
)


def tokenize(text: str | None) -> list[str]:
    return [
        token
        for token in _TOKEN.findall((text or "").lower())
        if len(token) > 1 and token not in STOPWORDS and not token.isdigit()
    ]


def document_terms(title: str | None, summary: str | None, tags: list[str]) -> list[str]:
    """Terms of one bookmark. Tags count as whole phrases as well as words,
    and count double: they were chosen to describe the bookmark."""
    terms = tokenize(title) * 2 + tokenize(summary)
    for tag in tags:
        words = tokenize(tag)
        terms += words * 2
        if tag.strip():
            terms += ["tag:" + " ".join(tag.lower().split())] * 2
    return terms


def text_hash(title: str | None, summary: str | None, tags: list[str]) -> str:
    joined = "\x1f".join([title or "", summary or "", *sorted(tags)])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:20]


def build_vectors(documents: list[list[str]], dim: int = DEFAULT_DIM, seed: int = 0):
    """LSA vectors for a list of tokenised documents.

    Returns ``(vectors, info)``: a float32 array with one unit-length row per
    document, and a dictionary describing how it was made. Rows of documents
    with no usable word are zero.
    """
    np = require_numpy()
    n = len(documents)
    if n == 0:
        return np.zeros((0, 1), dtype=np.float32), {"model": f"{MODEL_PREFIX}-0", "dim": 0}

    frequency = Counter()
    for terms in documents:
        frequency.update(set(terms))
    minimum = 2 if n >= 20 else 1
    kept = [term for term, count in frequency.items() if count >= minimum and count < 0.9 * n + 1]
    kept.sort(key=lambda term: (-frequency[term], term))
    kept = kept[:MAX_VOCABULARY]
    index = {term: position for position, term in enumerate(kept)}

    matrix = np.zeros((n, max(len(kept), 1)), dtype=np.float32)
    for row, terms in enumerate(documents):
        for term, count in Counter(terms).items():
            column = index.get(term)
            if column is not None:
                # Sublinear term frequency times inverse document frequency.
                matrix[row, column] = (1.0 + math.log(count)) * (
                    math.log((1 + n) / (1 + frequency[term])) + 1.0
                )
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    matrix /= np.where(norms > 0, norms, 1.0)

    dim = max(1, min(dim, n - 1 if n > 1 else 1, matrix.shape[1]))
    vectors = _truncated_svd(np, matrix, dim, seed)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = (vectors / np.where(norms > 0, norms, 1.0)).astype(np.float32)
    return vectors, {
        "model": f"{MODEL_PREFIX}-{dim}",
        "dim": int(dim),
        "vocabulary": len(kept),
        "documents": n,
        "empty_documents": int((norms[:, 0] == 0).sum()),
    }


def _truncated_svd(np, matrix, dim: int, seed: int):
    """Documents projected on the ``dim`` main directions (randomized SVD)."""
    n, width = matrix.shape
    if min(n, width) <= dim + 10:
        u, s, _ = np.linalg.svd(matrix, full_matrices=False)
        return u[:, :dim] * s[:dim]
    rng = np.random.default_rng(seed)
    sketch = matrix @ rng.standard_normal((width, dim + 10), dtype=np.float32)
    for _ in range(4):  # power iterations sharpen the leading directions
        sketch, _ = np.linalg.qr(sketch)
        sketch = matrix @ (matrix.T @ sketch)
    basis, _ = np.linalg.qr(sketch)
    u, s, _ = np.linalg.svd(basis.T @ matrix, full_matrices=False)
    return (basis @ u[:, :dim]) * s[:dim]
