"""Measure tinyvec: recall vs cost, the three filter strategies, int8 quantization, durability.

    python bench.py              # N=8000, d=64, ~1 minute
    python bench.py --n 20000    # bigger, slower

Ground truth is always exact brute force. recall@10 = |ANN top-10 ∩ true top-10| / 10.
"""
import argparse
import shutil
import time

import numpy as np

from tinyvec import Collection, ScalarQuantizer

TENANTS = [("big", 0.40), ("mid", 0.10), ("small", 0.01), ("tiny", 0.001)]


def data(n, d, seed=0):
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(200, d))
    x = centers[rng.integers(0, 200, n)] + 0.7 * rng.normal(size=(n, d))
    x = (x / np.linalg.norm(x, axis=1, keepdims=True)).astype(np.float32)
    rest = 1 - sum(f for _, f in TENANTS)
    names = [t for t, _ in TENANTS] + ["other"]
    probs = [f for _, f in TENANTS] + [rest]
    tenant = rng.choice(names, size=n, p=probs)
    q = x[rng.integers(0, n, 200)] + 0.3 * rng.normal(size=(200, d)).astype(np.float32)
    q = (q / np.linalg.norm(q, axis=1, keepdims=True)).astype(np.float32)
    return x, tenant, q


def truth(x, q, mask=None, k=10):
    d = 1 - x @ q
    if mask is not None:
        d = np.where(mask, d, np.inf)
    top = np.argsort(d)[:k]
    return {int(i) for i in top if np.isfinite(d[i])}


def table(rows, head):
    w = [max(len(str(r[i])) for r in rows + [head]) for i in range(len(head))]
    line = lambda r: "  ".join(str(c).rjust(w[i]) for i, c in enumerate(r))
    print(line(head)); print("  ".join("-" * x for x in w))
    for r in rows:
        print(line(r))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8000)
    ap.add_argument("--d", type=int, default=64)
    a = ap.parse_args()
    x, tenant, Q = data(a.n, a.d)
    path = "data/bench"
    shutil.rmtree(path, ignore_errors=True)
    col = Collection(path, a.d, M=16, ef_construction=100, fsync=False)
    t = time.time()
    for i in range(a.n):
        col.upsert(str(i), x[i], {"tenant": str(tenant[i])})
    build = time.time() - t
    print(f"\n## Build: {a.n} vectors, d={a.d}, M=16, ef_construction=100: {build:.1f}s "
          f"({a.n / build:.0f} inserts/s, pure Python)\n")

    # ---- 1. recall vs cost, unfiltered
    print("## 1. Unfiltered search: recall vs cost (200 queries, k=10)\n")
    rows = []
    t = time.time()
    for q in Q:
        truth(x, q)
    bf_ms = (time.time() - t) / len(Q) * 1000
    rows.append(["exact (numpy)", "1.000", a.n, f"{bf_ms:.2f}"])
    for ef in (10, 16, 32, 64, 128, 256):
        rec = evals = 0
        t = time.time()
        for q in Q:
            r = col.search(q, 10, ef=ef, mode="hnsw")
            rec += len({int(h.key) for h in r.hits} & truth(x, q)) / 10
            evals += r.dist_evals
        ms = (time.time() - t) / len(Q) * 1000 - bf_ms  # subtract the ground-truth cost
        rows.append([f"HNSW ef={ef}", f"{rec / len(Q):.3f}", evals // len(Q), f"{ms:.2f}"])
    table(rows, ["method", "recall@10", "dist evals/query", "ms/query"])
    print("\nNote: numpy brute force is one BLAS call, HNSW is a Python loop, so HNSW only wins on *distance\n"
          "computations* here. In a compiled engine at millions of vectors that becomes the wall-clock win.\n")

    # ---- 2. filtered search: three strategies
    print("## 2. Filtered search: tenant = X (k=10, ef=64)\n")
    rows = []
    for name, frac in TENANTS:
        mask = tenant == name
        for mode in ("post", "hnsw", "exact", "auto"):
            rec = evals = 0
            empty = 0
            chosen = {"exact": 0, "hnsw": 0}
            for q in Q:
                tr = truth(x, q, mask)
                r = col.search(q, 10, where={"tenant": name}, ef=64, mode=mode)
                got = {int(h.key) for h in r.hits}
                rec += len(got & tr) / max(len(tr), 1)
                evals += r.dist_evals
                empty += not got
                chosen["exact" if r.plan.startswith("exact") else "hnsw"] += 1
            label = mode if mode != "auto" else "auto -> " + ", ".join(f"{v} {k}" for k, v in chosen.items() if v)
            rows.append([f"{name} ({mask.sum()} pts, {frac:.1%})", label, f"{rec / len(Q):.3f}",
                         evals // len(Q), empty])
    table(rows, ["tenant", "strategy", "recall@10", "dist evals/query", "queries w/ 0 hits"])
    print("\npost-filter starves small tenants; filtered HNSW gets expensive as selectivity drops;\n"
          "exact via the payload index is perfect and cheap when few points match. `auto` picks per query.\n")

    # ---- 3. int8 scalar quantization
    print("## 3. int8 scalar quantization (brute force over all vectors)\n")
    sq = ScalarQuantizer.fit(x)
    codes = sq.encode(x)
    rows = [["float32", f"{x.nbytes / 1e6:.2f} MB", "1.000"]]
    for over in (1, 2, 4):
        rec = 0
        for q in Q:
            approx = sq.dots(q, codes)
            cand = np.argpartition(-approx, 10 * over)[: 10 * over]
            if over > 1:                                           # rescore with the originals
                cand = cand[np.argsort(-(x[cand] @ q))]
            top = set(int(i) for i in cand[:10]) if over > 1 else set(int(i) for i in cand[np.argsort(-approx[cand])][:10])
            rec += len(top & truth(x, q)) / 10
        rows.append([f"int8, top-{10 * over}" + (" + rescore" if over > 1 else ""), f"{codes.nbytes / 1e6:.2f} MB",
                     f"{rec / len(Q):.3f}"])
    table(rows, ["vectors as", "memory", "recall@10"])

    # ---- 4. durability
    print("\n## 4. Durability\n")
    rows = []
    for fs in (False, True):
        p = f"data/bench-wal-{fs}"
        shutil.rmtree(p, ignore_errors=True)
        c = Collection(p, a.d, fsync=fs)
        t = time.time()
        for i in range(300):
            c.upsert(f"k{i}", x[i], {"tenant": "t"})
        rows.append([f"upsert, fsync={'on' if fs else 'off'}", f"{(time.time() - t) / 300 * 1000:.2f} ms/op"])
        c.close()
    col.close()
    t = time.time()
    c = Collection(path, a.d)
    rows.append([f"recover: replay {c.recovery['replayed']} WAL records (rebuilds the graph)", f"{time.time() - t:.2f} s"])
    c.snapshot(); c.close()
    t = time.time()
    c = Collection(path, a.d)
    rows.append([f"recover: load snapshot seq={c.recovery['snapshot_seq']}, replay {c.recovery['replayed']}",
                 f"{time.time() - t:.2f} s"])
    table(rows, ["operation", "time"])
    print("\nfsync per write is the price of 'acknowledged = durable'. Snapshots turn recovery from\n"
          "'rebuild the whole index' into 'load a file'. Batching fsyncs (group commit) is the usual fix for the former.")


if __name__ == "__main__":
    main()
