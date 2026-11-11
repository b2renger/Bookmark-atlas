"""Analysis results in the database: vectors, clustering runs, comparisons."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import uuid
from collections import Counter

from atlas import __version__
from atlas.analysis import cluster, require_numpy, text
from atlas.db import now_iso

DEFAULT_ALGORITHM = "k-means"  # see docs/reports/e-means-test.md for why

# Clustering uses only the leading directions of the vectors. On the real
# collection, 16 gave the most separated and most repeatable clusters, while
# neighbour quality kept improving up to about 48 (docs/reports/e-means-test.md).
CLUSTER_DIMS = 16


def leading(matrix, dims: int = CLUSTER_DIMS):
    """The first ``dims`` directions of each vector, scaled back to unit length."""
    np = require_numpy()
    part = matrix[:, : max(1, min(dims, matrix.shape[1]))]
    norms = np.linalg.norm(part, axis=1, keepdims=True)
    return (part / np.where(norms > 0, norms, 1.0)).astype(np.float32)


def _documents(conn: sqlite3.Connection):
    """Live bookmarks with their terms and text hash, in a stable order."""
    tags: dict[str, list[str]] = {}
    for bookmark_id, name in conn.execute(
        "SELECT bookmark_id, name FROM bookmark_tags JOIN tags ON tags.id = tag_id ORDER BY name"
    ):
        tags.setdefault(bookmark_id, []).append(name)
    rows = conn.execute(
        "SELECT id, title, summary FROM bookmarks WHERE deleted_at IS NULL ORDER BY id"
    ).fetchall()
    ids = [row["id"] for row in rows]
    documents = [text.document_terms(r["title"], r["summary"], tags.get(r["id"], [])) for r in rows]
    hashes = [text.text_hash(r["title"], r["summary"], tags.get(r["id"], [])) for r in rows]
    return ids, documents, hashes, tags


def embed(conn: sqlite3.Connection, dim: int = text.DEFAULT_DIM) -> dict:
    """(Re)build the vector of every live bookmark. The vectors depend on the
    whole collection, so they are all rebuilt together."""
    np = require_numpy()
    ids, documents, hashes, _ = _documents(conn)
    vectors, info = text.build_vectors(documents, dim=dim)
    try:
        conn.execute("DELETE FROM embeddings WHERE model LIKE ?", (text.MODEL_PREFIX + "-%",))
        conn.executemany(
            "INSERT INTO embeddings (bookmark_id, model, dim, vector, text_hash) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (ids[i], info["model"], info["dim"], vectors[i].astype("<f4").tobytes(), hashes[i])
                for i in range(len(ids))
            ],
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    del np
    return info


def vectors_are_current(conn: sqlite3.Connection) -> bool:
    ids, _, hashes, _ = _documents(conn)
    stored = dict(
        conn.execute(
            "SELECT bookmark_id, text_hash FROM embeddings WHERE model LIKE ?",
            (text.MODEL_PREFIX + "-%",),
        )
    )
    return bool(ids) and stored == dict(zip(ids, hashes, strict=True))


def load_vectors(conn: sqlite3.Connection, *, refresh: bool = True):
    """``(ids, matrix, model)`` for the live bookmarks, rebuilding if they are stale."""
    np = require_numpy()
    if refresh and not vectors_are_current(conn):
        embed(conn)
    rows = conn.execute(
        "SELECT e.bookmark_id, e.model, e.dim, e.vector FROM embeddings e "
        "JOIN bookmarks b ON b.id = e.bookmark_id WHERE b.deleted_at IS NULL AND e.model LIKE ? "
        "ORDER BY e.bookmark_id",
        (text.MODEL_PREFIX + "-%",),
    ).fetchall()
    if not rows:
        raise ValueError("There are no bookmarks to analyse yet. Import some first.")
    matrix = np.frombuffer(b"".join(row["vector"] for row in rows), dtype="<f4").reshape(
        len(rows), rows[0]["dim"]
    )
    return [row["bookmark_id"] for row in rows], matrix.astype(np.float32), rows[0]["model"]


def candidate_ks(n: int) -> list[int]:
    """Numbers of clusters worth trying for a collection of ``n`` bookmarks:
    from broad themes to fine topics, small enough to browse."""
    upper = max(4, min(64, n // 8))
    return [k for k in (4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64) if k <= upper] or [2]


def suggest_k(conn: sqlite3.Connection, seed: int = 0) -> dict:
    """Try several numbers of clusters and report how separated each result is."""
    _, matrix, _ = load_vectors(conn)
    matrix = leading(matrix)
    curve = []
    for k in candidate_ks(len(matrix)):
        labels, _, metrics = cluster.fit(matrix, k, algorithm="k-means", seed=seed, restarts=2)
        curve.append(
            {"k": k, "silhouette": cluster.silhouette(matrix, labels), "mse": metrics["mse"]}
        )
    best = max(curve, key=lambda point: point["silhouette"])
    return {"suggested": best["k"], "curve": curve}


def cluster_run(
    conn: sqlite3.Connection,
    k: int | None = None,
    *,
    algorithm: str = DEFAULT_ALGORITHM,
    seed: int = 0,
    epochs: int | None = None,
) -> dict:
    """Cluster the collection and store the result as a run."""
    ids, matrix, model = load_vectors(conn)
    matrix = leading(matrix)
    suggestion = None
    if k is None:
        suggestion = suggest_k(conn, seed)
        k = suggestion["suggested"]
    labels, centroids, metrics = cluster.fit(
        matrix, k, algorithm=algorithm, seed=seed, epochs=epochs
    )
    metrics["silhouette"] = cluster.silhouette(matrix, labels)
    run_id = uuid.uuid4().hex
    params = {
        "k": int(k),
        "seed": seed,
        "vectors": model,
        "dims_used": int(matrix.shape[1]),
        "bookmarks": len(ids),
    }
    if epochs:
        params["epochs"] = epochs
    if suggestion:
        params["k_suggested_from"] = suggestion["curve"]
    try:
        conn.execute(
            "INSERT INTO runs (id, created_at, kind, algorithm, params, app_version, metrics) "
            "VALUES (?, ?, 'cluster', ?, ?, ?, ?)",
            (run_id, now_iso(), algorithm, json.dumps(params), __version__, json.dumps(metrics)),
        )
        conn.executemany(
            "INSERT INTO run_items (run_id, bookmark_id, cluster) VALUES (?, ?, ?)",
            [(run_id, ids[i], int(labels[i])) for i in range(len(ids))],
        )
        conn.execute(
            "INSERT INTO run_artifacts (run_id, name, data) VALUES (?, 'centroids', ?)",
            (run_id, centroids.astype("<f4").tobytes()),
        )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return describe_run(conn, run_id)


def list_runs(conn: sqlite3.Connection, kind: str = "cluster") -> list[dict]:
    runs = []
    for row in conn.execute(
        "SELECT * FROM runs WHERE kind = ? ORDER BY created_at DESC, rowid DESC", (kind,)
    ):
        params = json.loads(row["params"])
        runs.append(
            {
                "run_id": row["id"],
                "created_at": row["created_at"],
                "algorithm": row["algorithm"],
                "name": row["name"],
                "k": params.get("k"),
                "bookmarks": params.get("bookmarks"),
                "metrics": json.loads(row["metrics"] or "{}"),
            }
        )
    return runs


def latest_run_id(conn: sqlite3.Connection, kind: str = "cluster") -> str | None:
    runs = list_runs(conn, kind)
    return runs[0]["run_id"] if runs else None


def delete_run(conn: sqlite3.Connection, run_id: str) -> None:
    if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
        raise ValueError("No such run.")
    conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    conn.commit()


def set_cluster_label(
    conn: sqlite3.Connection, run_id: str, number: int, label: str | None
) -> None:
    if conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
        raise ValueError("No such run.")
    label = " ".join((label or "").split())[:120] or None
    conn.execute(
        "INSERT INTO cluster_edits (run_id, cluster, label) VALUES (?, ?, ?) "
        "ON CONFLICT(run_id, cluster) DO UPDATE SET label = excluded.label",
        (run_id, int(number), label),
    )
    conn.commit()


def describe_run(conn: sqlite3.Connection, run_id: str, samples: int = 5) -> dict:
    """A run with its clusters: size, telling tags, a generated or edited label,
    and the bookmarks closest to each centre."""
    np = require_numpy()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError("No such run.")
    params = json.loads(run["params"])
    members: dict[int, list[str]] = {}
    for bookmark_id, number in conn.execute(
        "SELECT ri.bookmark_id, ri.cluster FROM run_items ri JOIN bookmarks b "
        "ON b.id = ri.bookmark_id WHERE ri.run_id = ? AND b.deleted_at IS NULL",
        (run_id,),
    ):
        members.setdefault(number, []).append(bookmark_id)

    _, _, _, tags = _documents(conn)
    overall = Counter(tag for names in tags.values() for tag in names)
    total = max(1, sum(len(ids) for ids in members.values()))
    edits = {
        row["cluster"]: row["label"]
        for row in conn.execute(
            "SELECT cluster, label FROM cluster_edits WHERE run_id = ?", (run_id,)
        )
    }

    centroid_blob = conn.execute(
        "SELECT data FROM run_artifacts WHERE run_id = ? AND name = 'centroids'", (run_id,)
    ).fetchone()
    nearest: dict[int, list[str]] = {}
    if centroid_blob is not None:
        try:
            ids, matrix, _ = load_vectors(conn, refresh=False)
            matrix = leading(matrix, params.get("dims_used", CLUSTER_DIMS))
            centroids = np.frombuffer(centroid_blob[0], dtype="<f4").reshape(-1, matrix.shape[1])
            position = {bookmark_id: i for i, bookmark_id in enumerate(ids)}
            for number, bookmark_ids in members.items():
                rows = [position[b] for b in bookmark_ids if b in position]
                if rows and number < len(centroids):
                    d = ((matrix[rows] - centroids[number]) ** 2).sum(axis=1)
                    nearest[number] = [ids[rows[i]] for i in np.argsort(d)[:samples]]
        except ValueError:
            pass  # vectors were rebuilt with another size: samples are skipped

    clusters = []
    for number in sorted(members, key=lambda n: -len(members[n])):
        bookmark_ids = members[number]
        inside = Counter(tag for b in bookmark_ids for tag in tags.get(b, []))
        # A tag describes a cluster when it is frequent inside and rarer outside.
        scored = sorted(
            inside.items(),
            key=lambda item: (
                -item[1]
                * math.log(1.0 + (item[1] / len(bookmark_ids)) / (overall[item[0]] / total)),
                item[0],
            ),
        )
        top = [{"name": name, "count": count} for name, count in scored[:6]]
        titles = []
        for bookmark_id in nearest.get(number, bookmark_ids[:samples]):
            row = conn.execute(
                "SELECT title, url FROM bookmarks WHERE id = ?", (bookmark_id,)
            ).fetchone()
            titles.append(row["title"] or row["url"])
        clusters.append(
            {
                "cluster": number,
                "size": len(bookmark_ids),
                "label": edits.get(number) or " · ".join(t["name"] for t in top[:3]) or "Untitled",
                "edited": bool(edits.get(number)),
                "top_tags": top,
                "samples": titles,
            }
        )
    return {
        "run_id": run_id,
        "created_at": run["created_at"],
        "algorithm": run["algorithm"],
        "name": run["name"],
        "params": params,
        "metrics": json.loads(run["metrics"] or "{}"),
        "clusters": clusters,
    }


def compare(
    conn: sqlite3.Connection,
    ks: list[int] | None = None,
    seeds: int = 5,
    algorithms: tuple[str, ...] = cluster.ALGORITHMS,
    dims: int = CLUSTER_DIMS,
) -> dict:
    """Run every algorithm for several numbers of clusters and seeds.

    For each pair: loss (mean squared distance to the nearest centre), time,
    separation (silhouette), stability (agreement between seeds) and agreement
    with the k-means baseline.
    """
    ids, matrix, model = load_vectors(conn)
    matrix = leading(matrix, dims)
    ks = ks or [k for k in candidate_ks(len(matrix)) if k >= 8][:5]
    results = []
    for k in ks:
        fits = {
            name: [cluster.fit(matrix, k, algorithm=name, seed=seed) for seed in range(seeds)]
            for name in algorithms
        }
        for name in algorithms:
            runs = fits[name]
            labels = [run[0] for run in runs]
            pairs = [
                cluster.adjusted_rand_index(labels[i], labels[j])
                for i in range(seeds)
                for j in range(i + 1, seeds)
            ]
            against = (
                [
                    cluster.adjusted_rand_index(labels[s], fits["k-means"][s][0])
                    for s in range(seeds)
                ]
                if "k-means" in fits and name != "k-means"
                else []
            )
            losses = [run[2]["mse"] for run in runs]
            results.append(
                {
                    "k": k,
                    "algorithm": name,
                    "mse_mean": statistics.fmean(losses),
                    "mse_min": min(losses),
                    "mse_max": max(losses),
                    "seconds": statistics.fmean(run[2]["seconds"] for run in runs),
                    "silhouette": statistics.fmean(
                        cluster.silhouette(matrix, label) for label in labels
                    ),
                    "stability": statistics.fmean(pairs) if pairs else 1.0,
                    "agreement_with_kmeans": statistics.fmean(against) if against else None,
                    "smallest": min(run[2]["smallest"] for run in runs),
                    "largest": max(run[2]["largest"] for run in runs),
                    "empty": sum(run[2]["empty_clusters"] for run in runs),
                    "batch_size": runs[0][2].get("batch_size"),
                }
            )
    return {
        "bookmarks": len(ids),
        "vectors": f"{model}, first {matrix.shape[1]} directions",
        "seeds": seeds,
        "ks": ks,
        "results": results,
    }


def comparison_markdown(data: dict) -> str:
    lines = [
        f"{data['bookmarks']} bookmarks, vectors `{data['vectors']}`, "
        f"{data['seeds']} seeds per setting.",
        "",
        "| k | algorithm | loss (mean) | loss (min to max) | silhouette | stability | "
        "agrees with k-means | smallest / largest cluster | time |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in data["results"]:
        agreement = (
            "" if row["agreement_with_kmeans"] is None else f"{row['agreement_with_kmeans']:.2f}"
        )
        lines.append(
            f"| {row['k']} | {row['algorithm']} | {row['mse_mean']:.4f} | "
            f"{row['mse_min']:.4f} to {row['mse_max']:.4f} | {row['silhouette']:.3f} | "
            f"{row['stability']:.2f} | {agreement} | {row['smallest']} / {row['largest']} | "
            f"{row['seconds'] * 1000:.0f} ms |"
        )
    return "\n".join(lines) + "\n"
