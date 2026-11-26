# tinyvec: a vector database from scratch (Q4)

About 630 lines of Python and numpy. It implements the parts that make a vector database a *database* and not just a nearest-neighbour library:

| Part | File | What it does |
|---|---|---|
| **HNSW index** | `tinyvec/hnsw.py` | Layered small-world graph with greedy descent and beam search (`ef`), plus the paper's neighbour-diversity heuristic. Bounded degree, with numpy-batched distances per hop. |
| **Payload index + filters** | `tinyvec/collection.py` | Inverted index `field → value → ids`. Filters support `=`, `in`, list-contains and ranges. |
| **Cost-based query planner** | `tinyvec/collection.py` | Picks *exact scan of matching ids* or *filtered HNSW*, whichever is estimated cheaper. The cost per hop is learned from past searches. A naive *post-filter* mode is kept for comparison. |
| **Durability** | `tinyvec/store.py` | CRC'd write-ahead log, fsynced before the call returns (`F_FULLFSYNC` on macOS), plus atomic snapshots published by renaming a `CURRENT` file. Recovery = load snapshot + replay newer WAL records. A torn tail is cut; corruption in the middle is refused. |
| **int8 quantization** | `tinyvec/quant.py` | 4× less memory. Scores straight from the uint8 codes, then oversamples and rescores. |
| **Text embedder** | `tinyvec/embed.py` | Hashing-trick embedder (words + char 3-grams + bigrams), so the demo needs no model download. |

```bash
pip install numpy
python demo.py                     # semantic search over 3,000 K8s security alerts, 6 tenants, crash + recover
python bench.py                    # recall/cost curves, filter strategies, quantization, durability (~1 min)
python -m unittest test_tinyvec -v # 11 tests incl. 3 real SIGKILLs of a writer process
```

## Measured (MacBook, Python 3.14, N=8000, d=64; full output in `bench_results.txt`)

**Recall vs cost.** `ef` is the knob that trades recall against cost:

| method | recall@10 | distance computations / query |
|---|---|---|
| exact | 1.000 | 8000 |
| HNSW ef=16 | 0.759 | 441 |
| HNSW ef=64 | 0.963 | 1141 |
| HNSW ef=128 | 0.994 | 1940 |
| HNSW ef=256 | 1.000 | 3171 |

**Filtered search: why the planner exists** (k=10, ef=64)

| tenant share | post-filter recall | filtered HNSW cost | exact cost | `auto` picks |
|---|---|---|---|---|
| 40 % | 0.931 | 2277 | 3185 | HNSW (0.993) |
| 10 % | 0.636 | 5261 | **824** | exact (1.000) |
| 1 % | **0.052** (117/200 queries return nothing) | 8054 | **86** | exact (1.000) |
| 0.1 % | **0.011** (178/200 return nothing) | 8055 | **14** | exact (1.000) |

**int8 quantization:** 2.05 MB → 0.51 MB. Recall is 0.970 without rescoring and 1.000 when the top 20 are rescored with the float vectors.

**Durability:** 1.13 ms/upsert without fsync, 6.48 ms with a real flush to the drive. Recovery takes 14.2 s by replaying 8,000 WAL records, or 0.03 s by loading a snapshot.

## Things I found while building it
- **Exact duplicates break graph indexes.** My first synthetic alerts had only 365 unique vectors among 3,000. HNSW neighbour lists then filled up with identical copies, and the search missed the best hit. Real alerts carry pod hashes and PIDs. Production systems also dedupe or group.
- **"Approximate" is real.** In the demo, one brand-new outlier alert is missed at ef=64 and found at ef=256. That's why you measure recall and pair vector search with keyword search.
- **macOS `fsync` doesn't flush the drive.** With plain `fsync`, "durable" writes cost the same as non-durable ones (1.18 vs 1.14 ms). `F_FULLFSYNC` made the cost visible: 6.5 ms.
- **At small N, brute force wins.** One BLAS call over 8,000 vectors takes 0.46 ms. HNSW's advantage is in *distance computations*, which turn into wall-clock time only at millions of vectors in compiled code. The planner works this out on its own: for 3,000 alerts, the demo picks exact scans.

## Simplifications (on purpose)
Deleted points stay in the graph as tombstones, with no compaction. Single writer, no concurrency control. The HNSW graph is rebuilt by replaying the WAL rather than being logged itself. Range filters are checked per point and are not indexed.
