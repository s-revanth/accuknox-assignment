"""HNSW (Hierarchical Navigable Small World) index, after Malkov & Yashunin, 2016 (arXiv:1603.09320).

The idea in one paragraph: build a graph where every vector is linked to a handful of near neighbours.
To search, start somewhere and keep hopping to whichever neighbour is closer to the query; stop when no
neighbour is closer. A flat graph needs many hops, so HNSW stacks sparse "express" layers on top
(each node lives on layer L with probability ~ M^-L, like a skip list). Search greedily descends the
express layers to land near the target, then does a wider beam search (`ef` candidates) on layer 0.

Distances are cosine distance on unit vectors: d = 1 - a.b (0 = identical, 2 = opposite).
Everything is plain Python + numpy; neighbour distances are computed in one numpy batch per hop.
"""
from __future__ import annotations

import heapq
import math
import random
from typing import Callable, Iterable

import numpy as np

Accept = Callable[[int], bool]


class HNSW:
    def __init__(self, dim: int, M: int = 16, ef_construction: int = 100, seed: int = 0, capacity: int = 1024):
        self.dim, self.M, self.M0 = dim, M, 2 * M        # layer 0 gets 2*M links (paper's recommendation)
        self.ef_construction = ef_construction
        self.ml = 1.0 / math.log(M)                       # level multiplier: P(level >= L) = M^-L
        self.rng = random.Random(seed)
        self.vecs = np.zeros((capacity, dim), dtype=np.float32)
        self.n = 0
        self.levels: list[int] = []
        self.links: list[list[list[int]]] = []            # links[node][layer] -> neighbour ids
        self.entry, self.max_level = -1, -1
        self.dist_evals = 0                               # instrumentation: distance computations so far

    # ---------------------------------------------------------------- distances
    def _dist(self, q: np.ndarray, ids: list[int]) -> np.ndarray:
        self.dist_evals += len(ids)
        return 1.0 - self.vecs[ids] @ q

    # ---------------------------------------------------------------- core beam search on one layer
    def _search_layer(self, q, entry: list[tuple[float, int]], ef: int, layer: int,
                      accept: Accept | None = None) -> list[tuple[float, int]]:
        """Best-first search. `candidates` is a min-heap of nodes to expand; `results` a max-heap
        (negated) of the best `ef` *accepted* nodes. Nodes that fail `accept` are still traversed,
        so a filter never disconnects the graph; they just can't enter the result set."""
        visited = {i for _, i in entry}
        candidates = list(entry)
        heapq.heapify(candidates)
        results: list[tuple[float, int]] = []
        for d, i in entry:
            if accept is None or accept(i):
                heapq.heappush(results, (-d, i))
        while len(results) > ef:
            heapq.heappop(results)

        while candidates:
            d, c = heapq.heappop(candidates)
            if len(results) >= ef and d > -results[0][0]:
                break                                     # closest unexpanded is worse than our worst
            fresh = [x for x in self.links[c][layer] if x not in visited]
            if not fresh:
                continue
            visited.update(fresh)
            worst = -results[0][0] if len(results) >= ef else math.inf
            for nd, nb in zip(self._dist(q, fresh).tolist(), fresh):
                if nd < worst or len(results) < ef:
                    heapq.heappush(candidates, (nd, nb))
                    if accept is None or accept(nb):
                        heapq.heappush(results, (-nd, nb))
                        if len(results) > ef:
                            heapq.heappop(results)
                        worst = -results[0][0] if len(results) >= ef else math.inf
        return sorted((-nd, i) for nd, i in results)

    # ---------------------------------------------------------------- neighbour selection heuristic
    def _select(self, base: np.ndarray, cands: Iterable[tuple[float, int]], m: int) -> list[int]:
        """Paper's Algorithm 4. Keep candidate c only if it is closer to the base than to every
        neighbour already kept. This spreads links in different *directions* instead of picking m
        points from one dense cluster, which is what keeps the graph navigable between clusters.
        Pruned candidates back-fill any spare slots (keepPrunedConnections)."""
        kept: list[int] = []
        pruned: list[int] = []
        for d, c in sorted(cands):
            if len(kept) >= m:
                break
            if kept:
                to_kept = 1.0 - self.vecs[kept] @ self.vecs[c]
                self.dist_evals += len(kept)
                if not bool((to_kept > d).all()):
                    pruned.append(c)
                    continue
            kept.append(c)
        return kept + pruned[: m - len(kept)]

    # ---------------------------------------------------------------- insert
    def add(self, vec: np.ndarray) -> int:
        """Insert a unit vector; returns its internal id (0, 1, 2, ...)."""
        if self.n == len(self.vecs):
            self.vecs = np.vstack([self.vecs, np.zeros_like(self.vecs)])
        i = self.n
        self.vecs[i] = vec
        self.n += 1
        level = int(-math.log(1.0 - self.rng.random()) * self.ml)
        self.levels.append(level)
        self.links.append([[] for _ in range(level + 1)])
        if self.entry < 0:
            self.entry, self.max_level = i, level
            return i

        q = self.vecs[i]
        ep = [(float(self._dist(q, [self.entry])[0]), self.entry)]
        for layer in range(self.max_level, level, -1):    # 1) greedy descent through express layers
            ep = self._search_layer(q, ep, 1, layer)[:1]
        for layer in range(min(level, self.max_level), -1, -1):  # 2) link on every layer we live on
            found = self._search_layer(q, ep, self.ef_construction, layer)
            m_max = self.M0 if layer == 0 else self.M
            self.links[i][layer] = self._select(q, found, self.M)
            for nb in self.links[i][layer]:               # 3) back-links, pruning overfull lists
                lst = self.links[nb][layer]
                lst.append(i)
                if len(lst) > m_max:
                    nv = self.vecs[nb]
                    self.links[nb][layer] = self._select(nv, zip(self._dist(nv, lst).tolist(), lst), m_max)
            ep = found
        if level > self.max_level:
            self.entry, self.max_level = i, level
        return i

    # ---------------------------------------------------------------- query
    def search(self, q: np.ndarray, k: int, ef: int = 64, accept: Accept | None = None) -> list[tuple[float, int]]:
        if self.entry < 0:
            return []
        ep = [(float(self._dist(q, [self.entry])[0]), self.entry)]
        for layer in range(self.max_level, 0, -1):
            ep = self._search_layer(q, ep, 1, layer)[:1]
        return self._search_layer(q, ep, max(ef, k), 0, accept)[:k]

    # ---------------------------------------------------------------- persistence helpers
    def state(self) -> dict:
        return {"dim": self.dim, "M": self.M, "ef_construction": self.ef_construction, "n": self.n,
                "levels": self.levels, "links": self.links, "entry": self.entry, "max_level": self.max_level,
                "rng": self.rng.getstate()}

    @classmethod
    def from_state(cls, st: dict, vecs: np.ndarray) -> "HNSW":
        h = cls(st["dim"], st["M"], st["ef_construction"], capacity=max(len(vecs), 1))
        h.vecs[: len(vecs)] = vecs
        h.n, h.levels, h.links = st["n"], st["levels"], st["links"]
        h.entry, h.max_level = st["entry"], st["max_level"]
        rs = st["rng"]
        h.rng.setstate((rs[0], tuple(rs[1]), rs[2]))
        return h
