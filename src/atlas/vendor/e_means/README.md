# e-means (vendored)

Source: https://github.com/facebookresearch/faiss, folder `contrib/e_means`,
commit `f777f7d` (2026-11-10). License: MIT, see `LICENSE`.

Paper: Bessa et al., "e-means: outperforming k-means (in one-tenth the
epochs)", https://arxiv.org/abs/2611.09501

| File | Status |
| --- | --- |
| `base.py` | unmodified copy |
| `e_means.py` | one line changed: `import faiss` became an import of `_knn.py` |
| `_knn.py` | written for Bookmark Atlas, not part of Faiss |
| `__init__.py` | written for Bookmark Atlas |

Why vendored: the NumPy class of the reference implementation uses Faiss only
for `faiss.knn(x, centroids, 1)`. Replacing that call with an exact NumPy
search lets e-means run with NumPy alone, which keeps the installer small.
The algorithm itself is the reference code, untouched.

Only `NumpyEmeans` is included. The GPU, quantized, distributed and IVF
variants of the original folder are not.

To update: copy the two files again from Faiss and re-apply the one-line
change to `e_means.py`.
