"""A dependency-free text "embedder" using the hashing trick.

Not a neural model: it maps words and character 3-grams to dimensions via a hash, with a random sign,
then L2-normalises. Texts that share words or word fragments ("escape"/"escaped") land close together.
It is here so the demo runs offline with zero downloads; swap in a real model (e.g. a sentence-
transformer) by replacing `embed()` — the database does not care where vectors come from.
"""
import hashlib
import math
import re

import numpy as np

TOKEN = re.compile(r"[a-z0-9]+")


class HashingEmbedder:
    def __init__(self, dim: int = 256):
        self.dim = dim

    def _features(self, text: str):
        words = TOKEN.findall(text.lower())
        for w in words:
            yield "w:" + w, 1.0
            padded = f"<{w}>"
            for i in range(len(padded) - 2):
                yield "c:" + padded[i:i + 3], 0.5
        for a, b in zip(words, words[1:]):
            yield f"b:{a}_{b}", 0.7

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        counts: dict[str, float] = {}
        for feat, w in self._features(text):
            counts[feat] = counts.get(feat, 0.0) + w
        for feat, c in counts.items():
            h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
            v[h % self.dim] += (1.0 if (h >> 63) & 1 else -1.0) * (1.0 + math.log(c))
        n = np.linalg.norm(v)
        return v / n if n else v
