"""int8 scalar quantization: 4x less memory for the vectors, at a small recall cost.

Each dimension j is mapped linearly from [lo_j, hi_j] to the 256 codes 0..255:
    code = round((x - lo) / scale),   x ~= lo + code * scale
The dot product with a float query q never needs the floats back:
    q . x ~= q . lo + (q * scale) . code
so scoring N vectors is one (N x d) uint8 matrix times a float vector.
A real engine keeps the codes in RAM and the float originals on disk, scores everything with the codes,
then re-scores only the best few candidates with the originals ("oversample + rescore").
"""
import numpy as np


class ScalarQuantizer:
    def __init__(self, lo: np.ndarray, hi: np.ndarray):
        self.lo = lo.astype(np.float32)
        self.scale = np.maximum(hi - lo, 1e-12).astype(np.float32) / 255.0

    @classmethod
    def fit(cls, x: np.ndarray, quantile: float = 0.999) -> "ScalarQuantizer":
        # Clip at a high quantile rather than the true min/max, so one outlier doesn't waste codes.
        return cls(np.quantile(x, 1 - quantile, axis=0), np.quantile(x, quantile, axis=0))

    def encode(self, x: np.ndarray) -> np.ndarray:
        return np.clip(np.rint((x - self.lo) / self.scale), 0, 255).astype(np.uint8)

    def decode(self, codes: np.ndarray) -> np.ndarray:
        return self.lo + codes.astype(np.float32) * self.scale

    def dots(self, q: np.ndarray, codes: np.ndarray) -> np.ndarray:
        """Approximate q . x for every row of `codes`."""
        return float(q @ self.lo) + codes @ (q * self.scale)
