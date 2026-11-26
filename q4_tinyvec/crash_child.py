"""Helper for the crash test: upsert forever, print each key AFTER upsert() returns (= acknowledged).
The parent SIGKILLs this process at a random moment, then checks every acknowledged key survived."""
import sys

import numpy as np

from tinyvec import Collection

path, start = sys.argv[1], int(sys.argv[2])
col = Collection(path, dim=32, snapshot_every=50)       # snapshots happen mid-stream too
rng = np.random.default_rng(start)
i = start
while True:
    key = f"k{i}"
    col.upsert(key, rng.normal(size=32), {"i": i, "tenant": f"t{i % 3}"})
    print(key, flush=True)
    i += 1
