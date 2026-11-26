"""Collection = HNSW index + payload (metadata) index + query planner + WAL/snapshot durability.

    col = Collection("data/alerts", dim=256)
    col.upsert("a1", vec, {"tenant": "acme", "severity": "high", "ts": 1732000000})
    res = col.search(qvec, k=5, where={"tenant": "acme", "severity": {"in": ["high", "critical"]}})
    res.hits, res.plan

Filter language (`where`):
    {"field": value}                    equality (for list-valued fields: "contains")
    {"field": {"in": [v1, v2]}}         membership
    {"field": {"gte": a, "lt": b}}      range (gt/gte/lt/lte), evaluated per point, not indexed

Query planner (the interesting part). Three ways to answer a filtered query:
  * post-filter   : ANN top-ef without the filter, then drop non-matching. Fast, but a small tenant
                    gets *zero* results when none of its points happen to be in the global top-ef.
  * filtered HNSW : traverse the graph normally, but only matching points may enter the result set.
                    Good when many points match; when few match, the search wanders the whole graph.
  * exact scan    : use the payload index to list the matching ids and brute-force just those.
                    Perfect recall, and cheap exactly when few points match.
`auto` is a tiny cost-based planner: it estimates both costs in distance computations (exact = number
of matching ids from the payload index; filtered HNSW ~ unit_cost * ef / selectivity, with unit_cost
learned from past searches) and runs the cheaper one. bench.py measures all three strategies.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .hnsw import HNSW
from .store import WAL, read_snapshot, write_snapshot

RANGE_OPS = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b,
             "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b}


@dataclass
class Hit:
    key: str
    score: float            # cosine similarity, 1.0 = identical direction
    payload: dict


@dataclass
class Result:
    hits: list[Hit]
    plan: str
    dist_evals: int
    candidates: int | None = None
    extra: dict = field(default_factory=dict)


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(v))
    if not np.isfinite(n) or n == 0:
        raise ValueError("vector must be finite and non-zero")
    return v / n


class Collection:
    def __init__(self, path: str | Path, dim: int, M: int = 16, ef_construction: int = 100,
                 fsync: bool = True, snapshot_every: int | None = None):
        self.root = Path(path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.dim, self.snapshot_every = dim, snapshot_every
        self.unit_cost = 16.0                              # planner's starting guess, refined by observation
        self.index = HNSW(dim, M, ef_construction)
        self.keys: list[str | None] = []                 # internal id -> key (None = deleted)
        self.ids: dict[str, int] = {}                     # key -> internal id
        self.payloads: list[dict | None] = []
        self.pindex: dict[str, dict[Any, set[int]]] = {}  # field -> value -> internal ids
        self.seq = 0
        self.recovery = self._recover()
        self.wal = WAL(self.root / "wal.log", fsync)

    # ================================================================ writes
    def upsert(self, key: str, vector, payload: dict | None = None) -> int:
        v = _unit(vector)                                 # validate BEFORE logging
        if v.shape != (self.dim,):
            raise ValueError(f"expected dim {self.dim}, got {v.shape}")
        rec = {"seq": self.seq + 1, "op": "upsert", "key": key,
               "vec": v.tolist(), "payload": payload or {}}
        self.wal.append(rec)                              # durable first ...
        self._apply(rec)                                  # ... then visible
        self._maybe_snapshot()
        return rec["seq"]

    def delete(self, key: str) -> bool:
        if key not in self.ids:
            return False
        rec = {"seq": self.seq + 1, "op": "delete", "key": key}
        self.wal.append(rec)
        self._apply(rec)
        self._maybe_snapshot()
        return True

    def _apply(self, rec: dict) -> None:
        self.seq = rec["seq"]
        key = rec["key"]
        if key in self.ids:
            self._tombstone(self.ids[key])
        if rec["op"] == "upsert":
            i = self.index.add(np.asarray(rec["vec"], dtype=np.float32))
            self.keys.append(key)
            self.payloads.append(rec["payload"])
            self.ids[key] = i
            self._index_payload(i, rec["payload"])

    def _tombstone(self, i: int) -> None:
        """Deleted points stay in the graph as stepping stones (removing a node can disconnect
        neighbourhoods) but are invisible to results. A real engine compacts them periodically."""
        for f, vals in self._indexable(self.payloads[i]):
            for v in vals:
                self.pindex[f][v].discard(i)
        del self.ids[self.keys[i]]
        self.keys[i] = None
        self.payloads[i] = None

    @staticmethod
    def _indexable(payload: dict):
        for f, v in payload.items():
            vals = v if isinstance(v, list) else [v]
            vals = [x for x in vals if isinstance(x, (str, int, bool)) and not isinstance(x, float)]
            if vals:
                yield f, vals

    def _index_payload(self, i: int, payload: dict) -> None:
        for f, vals in self._indexable(payload):
            for v in vals:
                self.pindex.setdefault(f, {}).setdefault(v, set()).add(i)

    def __len__(self) -> int:
        return len(self.ids)

    # ================================================================ filters
    def _match(self, payload: dict | None, where: dict | None) -> bool:
        if payload is None:
            return False
        for f, cond in (where or {}).items():
            val = payload.get(f)
            if isinstance(cond, dict):
                if "in" in cond:
                    vals = val if isinstance(val, list) else [val]
                    if not any(x in cond["in"] for x in vals):
                        return False
                for op, ref in cond.items():
                    if op in RANGE_OPS and (val is None or not RANGE_OPS[op](val, ref)):
                        return False
            elif isinstance(val, list):
                if cond not in val:
                    return False
            elif val != cond:
                return False
        return True

    def _candidates(self, where: dict | None) -> set[int] | None:
        """Intersect payload-index postings for equality / `in` conditions. None = no index help."""
        sets = []
        for f, cond in (where or {}).items():
            posting = self.pindex.get(f, {})
            if isinstance(cond, dict) and "in" in cond:
                sets.append(set().union(*(posting.get(v, set()) for v in cond["in"])))
            elif not isinstance(cond, dict):
                sets.append(posting.get(cond, set()))
        if not sets:
            return None
        sets.sort(key=len)                               # intersect smallest first
        out = set(sets[0])
        for s in sets[1:]:
            out &= s
        return out

    # ================================================================ search
    def search(self, vector, k: int = 10, where: dict | None = None, ef: int = 64,
               mode: str = "auto") -> Result:
        q = _unit(vector)
        before = self.index.dist_evals
        cand = self._candidates(where) if where else None
        live = max(len(self.ids), 1)
        sel = (len(cand) / live) if cand is not None else 1.0
        est = ""
        if mode == "auto":
            # Cost model, in distance computations:
            #   exact         = number of candidates from the payload index
            #   filtered HNSW ~ unit_cost * ef / selectivity  (to collect ef matches you must visit
            #                   ~ef/sel nodes), capped at the collection size. unit_cost is learned.
            hnsw_cost = min(live, self.unit_cost * max(ef, k) / max(sel, 1e-9))
            if where and cand is not None and len(cand) <= hnsw_cost:
                mode = "exact"
            else:
                mode = "hnsw"
            est = f"; est. cost exact={len(cand) if cand is not None else live}, hnsw={hnsw_cost:.0f}"

        if mode == "exact":
            pool = list(cand) if cand is not None else list(self.ids.values())
            if where:
                pool = [i for i in pool if self._match(self.payloads[i], where)]
            if pool:
                d = 1.0 - self.index.vecs[pool] @ q
                self.index.dist_evals += len(pool)
                top = np.argsort(d)[:k] if len(pool) <= k else np.argpartition(d, k)[:k]
                found = sorted((float(d[j]), pool[j]) for j in top)
            else:
                found = []
            plan = f"exact scan over {len(pool)} candidate(s) from payload index"
        elif mode == "hnsw":
            accept = (lambda i: self.keys[i] is not None and self._match(self.payloads[i], where)) if where \
                else (lambda i: self.keys[i] is not None)
            found = self.index.search(q, k, ef, accept)
            spent = self.index.dist_evals - before
            # learn: observed cost per (ef slot / selectivity), smoothed
            self.unit_cost = 0.9 * self.unit_cost + 0.1 * (spent * sel / max(ef, k))
            note = f", {sel:.1%} match" if cand is not None else ""
            plan = f"filtered HNSW (ef={ef}{note})" if where else f"HNSW (ef={ef})"
        elif mode == "post":
            raw = self.index.search(q, max(ef, k), ef)
            found = [(d, i) for d, i in raw
                     if self.keys[i] is not None and self._match(self.payloads[i], where)][:k]
            plan = f"post-filter: ANN top-{max(ef, k)} then filter"
        else:
            raise ValueError(f"unknown mode {mode!r}")

        hits = [Hit(self.keys[i], 1.0 - d, self.payloads[i]) for d, i in found]
        return Result(hits, plan + est, self.index.dist_evals - before, None if cand is None else len(cand))

    # ================================================================ durability
    def snapshot(self) -> None:
        buf = io.BytesIO()
        np.save(buf, self.index.vecs[: self.index.n])
        st = {"seq": self.seq, "keys": self.keys, "payloads": self.payloads, "hnsw": self.index.state()}
        write_snapshot(self.root, self.seq, {"vectors.npy": buf.getvalue(),
                                             "state.json": json.dumps(st).encode()})
        self.wal.reset()

    def _maybe_snapshot(self) -> None:
        if self.snapshot_every and self.seq % self.snapshot_every == 0:
            self.snapshot()

    def _recover(self) -> dict:
        snap = read_snapshot(self.root)
        snap_seq = 0
        if snap:
            st = json.loads(snap["state.json"])
            vecs = np.load(io.BytesIO(snap["vectors.npy"]))
            self.index = HNSW.from_state(st["hnsw"], vecs)
            self.keys, self.payloads, self.seq = st["keys"], st["payloads"], st["seq"]
            self.ids = {k: i for i, k in enumerate(self.keys) if k is not None}
            for i, p in enumerate(self.payloads):
                if p is not None:
                    self._index_payload(i, p)
            snap_seq = self.seq
        records, torn = WAL.read(self.root / "wal.log")
        replayed = 0
        for rec in records:
            if rec["seq"] > self.seq:                     # older records are already in the snapshot
                self._apply(rec)
                replayed += 1
        return {"snapshot_seq": snap_seq, "replayed": replayed, "torn_bytes_cut": torn}

    def close(self) -> None:
        self.wal.close()
