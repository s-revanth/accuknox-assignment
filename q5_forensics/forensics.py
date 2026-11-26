"""Runtime-security forensics store on SQLite: ingest kernel-level events from Kubernetes pods, rebuild
process trees, detect with SQL policies, dedupe alerts, and investigate with recursive CTEs and windows.

    python forensics.py            # simulate an attack among normal traffic, print the investigation

Design notes are in README.md. The schema (schema.sql) carries the invariants; the named queries
(queries.sql) carry the analysis; this file is the thin ingestion path that ties them together.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
QUERIES = {m.group(1): m.group(2).strip()
           for m in re.finditer(r"-- name: (\w+)\n(.*?)(?=\n-- name: |\Z)", (HERE / "queries.sql").read_text(), re.S)}

SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}


class Forensics:
    def __init__(self, path: str | Path = ":memory:"):
        # isolation_level=None: we issue BEGIN ourselves, so a batch is exactly one transaction.
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")       # readers never block the writer
        self.db.execute("PRAGMA synchronous = NORMAL")     # durable at checkpoint; safe with WAL
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 30000")
        self.db.executescript((HERE / "schema.sql").read_text())

    # ------------------------------------------------------------------ setup helpers
    def tenant(self, name: str) -> int:
        return self.db.execute("INSERT INTO tenants(name) VALUES (?) ON CONFLICT(name) DO UPDATE SET name = name "
                               "RETURNING id", (name,)).fetchone()[0]

    def policy(self, name, kind, severity, match: dict, tenant: str | None = None) -> int:
        tid = self.tenant(tenant) if tenant else None
        return self.db.execute(
            "INSERT INTO policies(tenant_id, name, kind, severity, match) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(coalesce(tenant_id, 0), name) DO UPDATE SET kind = excluded.kind, "
            "severity = excluded.severity, match = excluded.match RETURNING id",
            (tid, name, kind, severity, json.dumps(match))).fetchone()[0]

    # ------------------------------------------------------------------ ingestion
    def ingest(self, events: list[dict]) -> int:
        """Ingest a batch atomically. BEGIN IMMEDIATE takes the write lock up front, so two writers
        can't both read-then-write and deadlock on lock upgrade. Returns number of alerts touched."""
        touched = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for ev in events:
                touched += self._ingest_one(ev)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return touched

    def _ingest_one(self, ev: dict) -> int:
        c = self.db
        tid = self.tenant(ev["tenant"])
        pod = c.execute(
            "INSERT INTO pods(tenant_id, cluster, namespace, name, image) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, cluster, namespace, name) DO UPDATE SET image = excluded.image RETURNING id",
            (tid, ev["cluster"], ev["namespace"], ev["pod"], ev.get("image", "?"))).fetchone()[0]
        kind, ts, pid = ev["kind"], ev["ts"], ev["pid"]
        live = "SELECT id FROM processes WHERE pod_id = ? AND pid = ? AND ended_at IS NULL"

        if kind == "exit":
            c.execute("UPDATE processes SET ended_at = ? WHERE pod_id = ? AND pid = ? AND ended_at IS NULL",
                      (ts, pod, pid))
            return 0
        if kind == "exec":
            # execve() replaces the image but keeps the pid; fork+exec creates a new pid. Either way the
            # previous live holder of this pid is finished (we may have missed its exit event).
            c.execute("UPDATE processes SET ended_at = ? WHERE pod_id = ? AND pid = ? AND ended_at IS NULL",
                      (ts, pod, pid))
            parent = c.execute(live, (pod, ev.get("ppid", 0))).fetchone()
            proc = c.execute(
                "INSERT INTO processes(pod_id, pid, parent_id, exe, args, uid, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (pod, pid, parent[0] if parent else None, ev["exe"], ev.get("args", ""), ev.get("uid", 0), ts)
            ).fetchone()[0]
        else:
            row = c.execute(live, (pod, pid)).fetchone()
            proc = row[0] if row else None

        skip = {"tenant", "cluster", "namespace", "pod", "image", "kind", "ts", "pid", "ppid", "exe", "args", "uid"}
        detail = {k: v for k, v in ev.items() if k not in skip}
        eid = c.execute("INSERT INTO events(tenant_id, pod_id, process_id, ts, kind, detail) "
                        "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
                        (tid, pod, proc, ts, kind, json.dumps(detail))).fetchone()[0]
        return c.execute(QUERIES["match_policies"], {"event_id": eid}).rowcount

    # ------------------------------------------------------------------ alert workflow
    def set_status(self, alert_id: int, status: str, actor: str) -> None:
        self.db.execute("UPDATE alerts SET status = ?, updated_by = ? WHERE id = ?", (status, actor, alert_id))

    # ------------------------------------------------------------------ investigation
    def q(self, name: str, **params) -> list[sqlite3.Row]:
        return self.db.execute(QUERIES[name], params).fetchall()

    def process_id(self, tenant: str, pod: str, exe: str) -> int:
        return self.db.execute(
            "SELECT pr.id FROM processes pr JOIN pods p ON p.id = pr.pod_id JOIN tenants t ON t.id = p.tenant_id "
            "WHERE t.name = ? AND p.name = ? AND pr.exe = ? ORDER BY pr.started_at LIMIT 1", (tenant, pod, exe)
        ).fetchone()[0]

    def plan(self, sql: str, params=()) -> str:
        return " | ".join(r[3] for r in self.db.execute("EXPLAIN QUERY PLAN " + sql, params))


DEFAULT_POLICIES = [
    ("shell-spawned-by-app-server", "exec", "critical",
     {"exe_in": ["/bin/sh", "/bin/bash", "/bin/dash"], "parent_exe_in": ["php-fpm", "node", "java", "python3", "nginx"]}),
    ("download-tool-executed", "exec", "high", {"exe_in": ["/usr/bin/curl", "/usr/bin/wget"]}),
    ("service-account-token-read", "file", "high", {"path_prefix": "/var/run/secrets/kubernetes.io/serviceaccount"}),
    ("write-under-etc", "file", "medium", {"path_prefix": "/etc/", "op_in": ["write"]}),
    ("mining-pool-port", "network", "critical", {"dst_port_in": [3333, 4444, 5555, 14444]}),
]


def install_default_policies(f: Forensics) -> None:
    for name, kind, sev, match in DEFAULT_POLICIES:
        f.policy(name, kind, sev, match)


if __name__ == "__main__":
    import simulate
    simulate.main()
