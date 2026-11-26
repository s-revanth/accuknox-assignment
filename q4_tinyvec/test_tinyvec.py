"""python -m unittest test_tinyvec -v"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from tinyvec import HNSW, Collection, HashingEmbedder, ScalarQuantizer
from tinyvec.store import WAL, CorruptWAL

HERE = Path(__file__).parent


def unit(rng, n, d):
    x = rng.normal(size=(n, d)).astype(np.float32)
    return x / np.linalg.norm(x, axis=1, keepdims=True)


class TmpDir(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.dir = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()


class TestHNSW(unittest.TestCase):
    def test_recall_and_exact_self_match(self):
        rng = np.random.default_rng(0)
        x = unit(rng, 2000, 32)
        h = HNSW(32, M=12, ef_construction=80)
        for v in x:
            h.add(v)
        rec = 0
        for i in range(0, 2000, 40):
            res = h.search(x[i], 10, ef=64)
            self.assertEqual(res[0][1], i)                       # a stored vector finds itself first
            truth = set(np.argsort(1 - x @ x[i])[:10])
            rec += len({j for _, j in res} & truth) / 10
        self.assertGreater(rec / 50, 0.95)

    def test_degree_bounded(self):
        rng = np.random.default_rng(1)
        h = HNSW(16, M=8)
        for v in unit(rng, 800, 16):
            h.add(v)
        for node in h.links:
            for layer, nbrs in enumerate(node):
                self.assertLessEqual(len(nbrs), 16 if layer == 0 else 8)
                self.assertEqual(len(nbrs), len(set(nbrs)))

    def test_filter_only_returns_accepted(self):
        rng = np.random.default_rng(2)
        h = HNSW(16)
        for v in unit(rng, 500, 16):
            h.add(v)
        res = h.search(unit(rng, 1, 16)[0], 10, ef=50, accept=lambda i: i % 7 == 0)
        self.assertTrue(res and all(i % 7 == 0 for _, i in res))


class TestCollection(TmpDir):
    def make(self, n=600, **kw):
        rng = np.random.default_rng(3)
        x = unit(rng, n, 24)
        col = Collection(self.dir, 24, fsync=False, **kw)
        for i in range(n):
            col.upsert(f"k{i}", x[i], {"tenant": f"t{i % 50}", "tags": ["a", "b"] if i % 2 else ["a"], "ts": i})
        return col, x

    def test_planner_and_filters_agree_with_ground_truth(self):
        col, x = self.make()
        q = x[5]
        for where in ({"tenant": "t5"}, {"tags": "b"}, {"tenant": {"in": ["t1", "t2"]}, "ts": {"gte": 100, "lt": 400}}):
            exact = [h.key for h in col.search(q, 5, where=where, mode="exact").hits]
            auto = [h.key for h in col.search(q, 5, where=where).hits]
            self.assertEqual(exact, auto, where)
            for h in col.search(q, 5, where=where).hits:
                self.assertTrue(col._match(h.payload, where))
        self.assertIn("exact", col.search(q, 5, where={"tenant": "t5"}).plan)     # 12 of 600 match

    def test_upsert_replaces_and_delete_hides(self):
        col, x = self.make(100)
        col.upsert("k1", x[50], {"tenant": "moved"})
        self.assertEqual(len(col), 100)
        self.assertEqual(col.search(x[50], 1, where={"tenant": "moved"}).hits[0].key, "k1")
        self.assertEqual([h.key for h in col.search(x[1], 5, where={"tenant": "t1"}).hits], ["k51"])  # old k1 gone
        self.assertTrue(col.delete("k50"))
        self.assertNotIn("k50", [h.key for h in col.search(x[50], 5).hits])
        self.assertFalse(col.delete("nope"))

    def test_rejects_bad_vectors_without_logging(self):
        col, _ = self.make(10)
        seq = col.seq
        with self.assertRaises(ValueError):
            col.upsert("bad", np.zeros(24))
        with self.assertRaises(ValueError):
            col.upsert("bad", np.ones(7))
        self.assertEqual(col.seq, seq)

    def test_recovery_is_exact_with_and_without_snapshot(self):
        col, x = self.make(300, snapshot_every=128)
        col.delete("k7")
        before = [(h.key, round(h.score, 6)) for h in col.search(x[9], 10).hits]
        col.close()
        again = Collection(self.dir, 24)
        self.assertEqual(again.recovery["snapshot_seq"], 256)
        self.assertEqual(again.recovery["replayed"], 45)
        self.assertEqual(len(again), 299)
        self.assertEqual([(h.key, round(h.score, 6)) for h in again.search(x[9], 10).hits], before)

    def test_torn_tail_is_cut_and_middle_corruption_refused(self):
        col, _ = self.make(20)
        col.close()
        wal = self.dir / "wal.log"
        with open(wal, "ab") as f:
            f.write(b"deadbeef\t{\"seq\":21,\"op\":\"upse")         # died mid-append
        again = Collection(self.dir, 24)
        self.assertEqual((len(again), again.recovery["torn_bytes_cut"] > 0), (20, True))
        again.close()
        lines = wal.read_bytes().split(b"\n")
        lines[3] = lines[3][:-5] + b"XXXXX"                          # flip bytes in the middle
        wal.write_bytes(b"\n".join(lines))
        with self.assertRaises(CorruptWAL):
            WAL.read(wal)


class TestCrash(TmpDir):
    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "needs POSIX SIGKILL")
    def test_sigkill_loses_no_acknowledged_write(self):
        """Run a writer process, SIGKILL it at random moments 3 times, check every ack survived."""
        acked = []
        for round_ in range(3):
            p = subprocess.Popen([sys.executable, str(HERE / "crash_child.py"), str(self.dir), str(round_ * 100_000)],
                                 stdout=subprocess.PIPE, text=True, cwd=HERE)
            deadline = time.time() + 0.6 + 0.4 * round_
            while time.time() < deadline:
                line = p.stdout.readline()
                if line:
                    acked.append(line.strip())
            os.kill(p.pid, signal.SIGKILL)
            p.wait()
            p.stdout.close()
        self.assertGreater(len(acked), 50)
        col = Collection(self.dir, 32)
        missing = [k for k in acked if k not in col.ids]
        self.assertEqual(missing, [], f"{len(missing)} acknowledged writes lost")
        # nothing that wasn't written appears either: every key was produced by some round
        self.assertTrue(all(k.startswith("k") for k in col.ids))


class TestQuantAndEmbed(unittest.TestCase):
    def test_int8_roundtrip_error_small(self):
        x = unit(np.random.default_rng(4), 1000, 64)
        sq = ScalarQuantizer.fit(x)
        q = x[0]
        err = np.abs(sq.dots(q, sq.encode(x)) - x @ q).max()
        self.assertLess(err, 0.05)

    def test_embedder_similarity_ordering(self):
        e = HashingEmbedder(256)
        a, b, c = (e.embed(t) for t in ("shell spawned in container", "bash shell spawned inside a container",
                                        "quarterly revenue report"))
        self.assertGreater(a @ b, a @ c)
        self.assertAlmostEqual(float(np.linalg.norm(a)), 1.0, places=5)


if __name__ == "__main__":
    unittest.main()
