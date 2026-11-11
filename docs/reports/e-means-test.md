# e-means on a real bookmark collection

Written 2026-11-11, for phase 3. Reproduce with `atlas compare`.

## The question

Is e-means (Bessa et al., November 2026, https://arxiv.org/abs/2611.09501)
worth using as the default clustering engine for a personal bookmark
collection?

## Answer

**Not at this size. It is as good as k-means, not better, and k-means stays
the default.** e-means remains available as a method in the Clusters tab.

On 956 bookmarks the two methods give the same quality: losses within 1% of
each other in both directions, the same separation, the same repeatability.
The paper's advantage is speed on millions of vectors with thousands of
clusters. A thousand vectors is clustered in a few hundredths of a second by
either method, so there is nothing for e-means to win here.

## What was compared

- **e-means**: the reference implementation from the Faiss repository
  (`contrib/e_means`, commit `f777f7d`), `NumpyEmeans`, default settings
  (5 passes, random starting points, batch size and step chosen by the
  paper's scaling laws). The code is vendored unmodified except for the
  nearest-centroid search, done with NumPy instead of Faiss. Both are exact
  searches.
- **e-means++**: the same, started from k-means++ centres.
- **k-means**: Lloyd's algorithm with k-means++ starts, best of 3 starts, up
  to 25 iterations. Written for this project and checked against
  scikit-learn (same loss to 0.1%).

Data: the 956 bookmarks of the real collection after grooming. Vectors: latent
semantic analysis of title, summary and tags (`lsa-48`), of which the first 16
directions are used for clustering (see "Choosing the vectors" below). Eight
random seeds per setting.

Measures:

- **loss**: mean squared distance of a bookmark to its cluster centre. What
  both methods try to minimise. Lower is better.
- **silhouette**: how much closer bookmarks are to their own cluster than to
  the next one. 0 is overlapping, 1 is clearly apart.
- **stability**: agreement (adjusted Rand index) between runs that differ only
  by their random seed. 1 means the same clusters every time.
- **agrees with k-means**: the same agreement, measured against the k-means
  run with the same seed.

## Results

956 bookmarks, vectors `lsa-48, first 16 directions`, 8 seeds per setting.

| k | algorithm | loss (mean) | loss (min to max) | silhouette | stability | agrees with k-means | smallest / largest cluster | time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 8 | e-means | 0.4631 | 0.4512 to 0.4764 | 0.183 | 0.57 | 0.53 | 31 / 258 | 122 ms |
| 8 | e-means++ | 0.4570 | 0.4507 to 0.4690 | 0.189 | 0.59 | 0.67 | 40 / 199 | 95 ms |
| 8 | k-means | 0.4612 | 0.4514 to 0.4699 | 0.184 | 0.57 |  | 36 / 224 | 14 ms |
| 12 | e-means | 0.3695 | 0.3628 to 0.3769 | 0.222 | 0.68 | 0.67 | 27 / 125 | 120 ms |
| 12 | e-means++ | 0.3713 | 0.3686 to 0.3787 | 0.219 | 0.67 | 0.66 | 28 / 134 | 109 ms |
| 12 | k-means | 0.3700 | 0.3638 to 0.3735 | 0.224 | 0.65 |  | 32 / 158 | 11 ms |
| 16 | e-means | 0.3008 | 0.2957 to 0.3069 | 0.250 | 0.85 | 0.82 | 24 / 102 | 108 ms |
| 16 | e-means++ | 0.3051 | 0.3001 to 0.3085 | 0.242 | 0.79 | 0.78 | 24 / 104 | 129 ms |
| 16 | k-means | 0.3035 | 0.2962 to 0.3166 | 0.246 | 0.79 |  | 27 / 101 | 13 ms |
| 24 | e-means | 0.2527 | 0.2501 to 0.2578 | 0.220 | 0.75 | 0.75 | 9 / 86 | 124 ms |
| 24 | e-means++ | 0.2518 | 0.2481 to 0.2603 | 0.222 | 0.77 | 0.76 | 9 / 85 | 126 ms |
| 24 | k-means | 0.2535 | 0.2513 to 0.2570 | 0.226 | 0.76 |  | 10 / 85 | 20 ms |
| 32 | e-means | 0.2274 | 0.2228 to 0.2325 | 0.206 | 0.68 | 0.68 | 5 / 86 | 115 ms |
| 32 | e-means++ | 0.2289 | 0.2271 to 0.2313 | 0.205 | 0.68 | 0.67 | 5 / 79 | 125 ms |
| 32 | k-means | 0.2295 | 0.2273 to 0.2309 | 0.205 | 0.67 |  | 2 / 79 | 19 ms |
| 48 | e-means | 0.1955 | 0.1924 to 0.1982 | 0.189 | 0.63 | 0.58 | 3 / 60 | 125 ms |
| 48 | e-means++ | 0.1949 | 0.1923 to 0.1987 | 0.187 | 0.65 | 0.60 | 3 / 65 | 138 ms |
| 48 | k-means | 0.1999 | 0.1987 to 0.2022 | 0.189 | 0.59 |  | 1 / 58 | 29 ms |

Reading the table:

- **Loss.** No method wins. e-means is lower at k = 12, 16, 24, 32 and 48;
  k-means is lower at k = 8. The gaps are under 2% except at k = 48 (2.2% in
  favour of e-means), and smaller than the spread between seeds.
- **Stability.** Best at k = 16 for everyone: 0.85 for e-means, 0.79 for the
  other two. Elsewhere the three are within 0.04 of each other.
- **Time.** k-means 11 to 29 ms, e-means 108 to 125 ms. Both are instant. The
  reference e-means carries fixed overhead that only pays off on large data.
- **Cluster sizes.** Comparable. At k = 8 plain e-means produced a larger
  catch-all cluster (258 against 224).

## Where e-means is weaker: very small sets

On a toy collection of 72 bookmarks in 3 clean topics (the one used by the
test suite), counting how often each method recovers the 3 topics exactly over
20 seeds:

| method | recovers the 3 topics |
| --- | --- |
| k-means | 20 of 20 |
| e-means++ | 17 of 20 |
| e-means | 15 of 20 |

And on 3,000 synthetic points in 12 well-separated groups, raising e-means
from 5 to 25 passes made it worse: the scaling laws then chose a batch of
1,024 points, a third of the data, leaving three updates per pass. The paper's
experiments start at a million vectors; these sizes are far outside them, so
this is extrapolation of the laws, not a defect in the method.

## Choosing the vectors

The choice of vectors mattered far more than the choice of algorithm.

| directions used | neighbours sharing a tag | silhouette (k = 16) | stability (k = 16) |
| --- | --- | --- | --- |
| 8 | 0.34 | 0.26 | 0.69 |
| 16 | 0.39 | 0.24 | 0.80 |
| 24 | 0.41 | 0.18 | 0.69 |
| 32 | 0.41 | 0.15 | 0.59 |
| 48 | 0.43 | 0.11 | 0.51 |
| 64 | 0.42 | 0.08 | 0.44 |
| 128 | 0.43 | 0.05 | 0.39 |

- "Neighbours sharing a tag" is measured on vectors built from title and
  summary only, tags left out: the share of a bookmark's 10 nearest neighbours
  that have at least one tag in common with it. Picking neighbours at random
  gives 0.08. It keeps improving up to about 48 directions.
- Clusters are most separated and most repeatable with 16 directions.

So 48 directions are stored (for neighbour features later) and clustering uses
the first 16.

## Choosing the number of clusters

Silhouette of k-means for each candidate: 4: 0.14, 6: 0.16, 8: 0.18, 10: 0.21,
12: 0.22, **16: 0.25**, 20: 0.25, 24: 0.23, 32: 0.20, 40: 0.20, 48: 0.18,
64: 0.17. "Auto" picks 16. The 16 clusters read as topics: Gaussian splatting,
local language models, 3D reconstruction, world models, ComfyUI workflows,
open-source tools, video generation, coding agents, Blender and 3D printing,
Three.js and WebGL, and so on.

## Limits of this test

- One collection, one kind of vector. LSA only knows which words appear
  together in this collection; a neural sentence-embedding model would likely
  give better separated clusters, and the comparison should be rerun then.
- A silhouette of 0.25 is modest: topics overlap, which is expected of a
  personal reading list.
- The e-means used here is the NumPy reference, not the GPU implementation the
  paper times.
- No timing claim from the paper was tested; the data is five orders of
  magnitude too small for that.
