import pytest

np = pytest.importorskip("numpy")

from atlas.analysis import cluster, text  # noqa: E402


def blobs(n=600, k=6, d=16, spread=6.0, seed=0):
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(k, d)) * spread
    truth = rng.integers(0, k, n)
    return (centres[truth] + rng.normal(size=(n, d))).astype("float32"), truth


def test_tokenize_keeps_technical_words_and_drops_filler():
    assert text.tokenize("The NEW Three.js and C++ tools for 3D Gaussian-Splatting, v2!") == [
        "three.js",
        "c++",
        "tools",
        "3d",
        "gaussian-splatting",
        "v2",
    ]
    assert text.tokenize(None) == []


def test_document_terms_count_tags_as_phrases():
    terms = text.document_terms("Lantern", "A renderer.", ["Path Tracing"])
    assert terms.count("tag:path tracing") == 2 and "path" in terms and "renderer" in terms


def test_text_hash_changes_with_content_not_tag_order():
    assert text.text_hash("a", "b", ["x", "y"]) == text.text_hash("a", "b", ["y", "x"])
    assert text.text_hash("a", "b", ["x"]) != text.text_hash("a", "c", ["x"])


def test_vectors_put_related_documents_together():
    topics = {
        "splat": "gaussian splatting radiance field reconstruction scene capture",
        "llm": "language model prompt agent token inference chat",
        "audio": "music synthesizer audio sound sample track",
    }
    rng = np.random.default_rng(0)
    docs, truth = [], []
    for index, words in enumerate(topics.values()):
        vocabulary = words.split()
        for _ in range(30):
            docs.append(list(rng.choice(vocabulary, 5)) + ["tool"])
            truth.append(index)
    vectors, info = text.build_vectors(docs, dim=8)

    assert vectors.shape == (90, 8) and info["model"] == "lsa-8"
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-4)
    labels, _, _ = cluster.fit(vectors, 3, algorithm="k-means", seed=1)
    assert cluster.adjusted_rand_index(labels, truth) > 0.95


def test_vectors_cope_with_tiny_and_empty_input():
    vectors, info = text.build_vectors([])
    assert len(vectors) == 0
    vectors, info = text.build_vectors([["alpha", "beta"], [], ["alpha", "gamma"]], dim=64)
    assert vectors.shape[0] == 3 and info["empty_documents"] == 1
    assert np.isfinite(vectors).all() and float(np.abs(vectors[1]).sum()) == 0.0


@pytest.mark.parametrize("algorithm", cluster.ALGORITHMS)
def test_each_engine_returns_consistent_results(algorithm):
    x, _ = blobs()
    labels, centroids, metrics = cluster.fit(x, 6, algorithm=algorithm, seed=3)

    assert labels.shape == (600,) and centroids.shape == (6, 16)
    assert set(np.unique(labels)) <= set(range(6))
    # Labels are the nearest final centroid, and the loss is the one they give.
    again, mse = cluster.assign(np, x, centroids)
    assert (again == labels).all() and metrics["mse"] == pytest.approx(mse)
    assert metrics["seconds"] >= 0 and metrics["largest"] >= metrics["smallest"]


def test_kmeans_finds_well_separated_groups_and_matches_scikit_learn():
    x, truth = blobs()
    labels, _, metrics = cluster.fit(x, 6, algorithm="k-means", seed=0)
    assert cluster.adjusted_rand_index(labels, truth) == pytest.approx(1.0)

    sklearn_cluster = pytest.importorskip("sklearn.cluster")
    reference = sklearn_cluster.KMeans(6, n_init=5, random_state=0).fit(x)
    assert metrics["mse"] == pytest.approx(reference.inertia_ / len(x), rel=1e-3)


def test_vendored_emeans_runs_without_faiss_and_can_reach_the_optimum():
    x, truth = blobs()
    best = max(
        cluster.adjusted_rand_index(cluster.fit(x, 6, algorithm="e-means++", seed=s)[0], truth)
        for s in range(3)
    )
    assert best > 0.99


def test_numpy_nearest_centroid_search_is_exact():
    from atlas.vendor.e_means import _knn

    x, _ = blobs(n=500)
    centroids = x[:7]
    distances, labels = _knn.knn(x, centroids, 1)
    brute = ((x[:, None, :] - centroids[None]) ** 2).sum(-1)
    assert distances.shape == (500, 1) and labels.dtype == np.int64
    assert (labels[:, 0] == brute.argmin(1)).all()
    assert np.allclose(distances[:, 0], brute.min(1), atol=1e-2)
    with pytest.raises(NotImplementedError):
        _knn.knn(x, centroids, 2)


def test_fit_rejects_bad_requests():
    x, _ = blobs(n=20)
    with pytest.raises(ValueError, match="between 2 and 20"):
        cluster.fit(x, 21)
    with pytest.raises(ValueError, match="between 2 and 20"):
        cluster.fit(x, 1)
    with pytest.raises(ValueError, match="Unknown algorithm"):
        cluster.fit(x, 3, algorithm="magic")


def test_adjusted_rand_index():
    a = [0, 0, 1, 1, 2, 2]
    assert cluster.adjusted_rand_index(a, a) == pytest.approx(1.0)
    assert cluster.adjusted_rand_index(a, [5, 5, 9, 9, 7, 7]) == pytest.approx(
        1.0
    )  # names do not matter
    assert cluster.adjusted_rand_index(a, [0, 1, 2, 0, 1, 2]) < 0.1
    sklearn_metrics = pytest.importorskip("sklearn.metrics")
    rng = np.random.default_rng(0)
    p, q = rng.integers(0, 5, 300), rng.integers(0, 4, 300)
    assert cluster.adjusted_rand_index(p, q) == pytest.approx(
        sklearn_metrics.adjusted_rand_score(p, q)
    )


def test_silhouette_matches_scikit_learn():
    x, truth = blobs(n=300)
    ours = cluster.silhouette(x, truth)
    assert ours > 0.5
    sklearn_metrics = pytest.importorskip("sklearn.metrics")
    assert ours == pytest.approx(sklearn_metrics.silhouette_score(x, truth), abs=1e-3)
    assert cluster.silhouette(x, np.zeros(300, dtype=int)) == 0.0
