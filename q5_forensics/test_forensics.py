"""python -m unittest test_forensics -v"""
import multiprocessing as mp
import sqlite3
import tempfile
import unittest
from pathlib import Path

import simulate
from forensics import QUERIES, Forensics, install_default_policies

B = {"tenant": "acme", "cluster": "c1", "namespace": "ns", "pod": "web-1", "image": "php"}


def miner_hits(path, n):
    f = Forensics(path)
    for i in range(n):  # one transaction per event: maximum lock contention
        f.ingest([{**B, "kind": "network", "ts": 2000 + i, "pid": 50, "dst_ip": "1.2.3.4", "dst_port": 3333}])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "f.db"
        self.f = Forensics(self.path)
        install_default_policies(self.f)
        self.f.ingest([{**B, "kind": "exec", "ts": 1000, "pid": 1, "ppid": 0, "exe": "/pause"},
                       {**B, "kind": "exec", "ts": 1001, "pid": 7, "ppid": 1, "exe": "php-fpm"},
                       {**B, "kind": "exec", "ts": 1002, "pid": 50, "ppid": 7, "exe": "/bin/sh"}])

    def tearDown(self):
        self.f.db.close()
        self.tmp.cleanup()

    def alerts(self):
        return self.f.db.execute("SELECT p.name, a.hits, a.status FROM alerts a JOIN policies p ON p.id = a.policy_id "
                                 "ORDER BY a.id").fetchall()


class TestDetection(Base):
    def test_shell_from_app_server_alerts_once(self):
        self.assertEqual([tuple(r) for r in self.alerts()], [("shell-spawned-by-app-server", 1, "open")])

    def test_concurrent_writers_lose_no_increment(self):
        procs = [mp.get_context("spawn").Process(target=miner_hits, args=(str(self.path), 50)) for _ in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        rows = self.f.db.execute("SELECT hits FROM alerts a JOIN policies p ON p.id = a.policy_id "
                                 "WHERE p.name = 'mining-pool-port'").fetchall()
        self.assertEqual([r[0] for r in rows], [200])   # one alert, every hit counted

    def test_resolved_alert_reopens_as_new_row(self):
        aid = self.f.db.execute("SELECT id FROM alerts").fetchone()[0]
        self.f.set_status(aid, "resolved", "a")
        self.f.ingest([{**B, "kind": "exec", "ts": 1100, "pid": 51, "ppid": 7, "exe": "/bin/sh"}])
        self.assertEqual([tuple(r) for r in self.alerts()],
                         [("shell-spawned-by-app-server", 1, "resolved"), ("shell-spawned-by-app-server", 1, "open")])

    def test_tenant_policy_does_not_leak(self):
        self.f.policy("acme-only-nc", "exec", "high", {"exe_in": ["/usr/bin/nc"]}, tenant="acme")
        other = {**B, "tenant": "globex"}
        self.f.ingest([{**other, "kind": "exec", "ts": 1, "pid": 9, "ppid": 0, "exe": "/usr/bin/nc"},
                       {**B, "kind": "exec", "ts": 1, "pid": 9, "ppid": 0, "exe": "/usr/bin/nc"}])
        names = [r[0] for r in self.f.db.execute(
            "SELECT t.name FROM alerts a JOIN tenants t ON t.id = a.tenant_id JOIN policies p ON p.id = a.policy_id "
            "WHERE p.name = 'acme-only-nc'")]
        self.assertEqual(names, ["acme"])


class TestIntegrity(Base):
    def test_events_are_append_only(self):
        with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
            self.f.db.execute("UPDATE events SET ts = 0")

    def test_status_machine_and_audit(self):
        aid = self.f.db.execute("SELECT id FROM alerts").fetchone()[0]
        self.f.set_status(aid, "acknowledged", "priya")
        with self.assertRaisesRegex(sqlite3.DatabaseError, "illegal"):
            self.f.set_status(aid, "open", "x")
        self.f.set_status(aid, "resolved", "priya")
        hist = self.f.db.execute("SELECT from_status, to_status, actor FROM alert_history").fetchall()
        self.assertEqual([tuple(r) for r in hist], [("open", "acknowledged", "priya"), ("acknowledged", "resolved", "priya")])

    def test_failed_batch_rolls_back_entirely(self):
        before = self.f.db.execute("SELECT count(*) FROM events").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.f.ingest([{**B, "kind": "file", "ts": 5, "pid": 50, "path": "/x"},
                           {**B, "kind": "bogus", "ts": 6, "pid": 50}])        # CHECK(kind IN ...) fails
        self.assertEqual(self.f.db.execute("SELECT count(*) FROM events").fetchone()[0], before)

    def test_pid_reuse_keeps_trees_separate(self):
        self.f.ingest([{**B, "kind": "exit", "ts": 1003, "pid": 50},
                       {**B, "kind": "exec", "ts": 1004, "pid": 60, "ppid": 1, "exe": "/usr/bin/cron"},
                       {**B, "kind": "exec", "ts": 1005, "pid": 50, "ppid": 60, "exe": "/usr/bin/logrotate"}])
        new = self.f.process_id("acme", "web-1", "/usr/bin/logrotate")
        chain = [r["exe"] for r in self.f.q("ancestry", process_id=new)]
        self.assertEqual(chain, ["/pause", "/usr/bin/cron", "/usr/bin/logrotate"])   # not php-fpm's shell

    def test_retention_cascades_to_fts_and_alerts(self):
        self.f.q("retention", cutoff=10**10, batch=1000)
        self.assertEqual(self.f.db.execute("SELECT count(*) FROM events_fts").fetchone()[0], 0)
        self.assertIsNone(self.f.db.execute("SELECT first_event_id FROM alerts").fetchone()[0])


class TestPlans(Base):
    def test_hot_queries_use_indexes(self):
        p = self.f.plan
        self.assertIn("processes_live", p("SELECT id FROM processes WHERE pod_id = ? AND pid = ? AND ended_at IS NULL", (1, 1)))
        self.assertIn("events_dst_ip", p(QUERIES["who_contacted"], {"ip": "1.2.3.4"}))
        self.assertIn("events_tenant_ts", p("SELECT * FROM events WHERE tenant_id = ? AND ts > ?", (1, 0)))
        self.assertIn("alerts_one_active", p("SELECT id FROM alerts WHERE tenant_id = 1 AND fingerprint = 'x' "
                                              "AND status <> 'resolved'"))


class TestScenario(unittest.TestCase):
    def test_simulation_finds_the_attack(self):
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            f = simulate.main([])
        chain = f.q("kill_chain", window=60)
        self.assertEqual({(r["connecting_exe"], r["dst"]) for r in chain},
                         {("/usr/bin/curl", "45.33.12.9:80"), ("/usr/bin/wget", "45.33.12.9:80"), ("/tmp/.k", "45.33.12.9:3333")})
        self.assertEqual(f.q("search", q="secrets", tenant_id=f.tenant("globex"), limit=5), [])  # tenant-scoped


if __name__ == "__main__":
    unittest.main()
