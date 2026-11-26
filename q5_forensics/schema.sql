-- Runtime-security forensics store (SQLite 3.38+). Every constraint here is a rule the database enforces
-- even if application code is buggy: that is the point of putting it in the schema.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tenants (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS pods (
    id        INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    cluster   TEXT NOT NULL,
    namespace TEXT NOT NULL,
    name      TEXT NOT NULL,
    image     TEXT NOT NULL,
    UNIQUE (tenant_id, cluster, namespace, name)
);

-- The process tree. PIDs are recycled by the kernel, so (pod, pid) is NOT an identity;
-- (pod, pid, started_at) is. parent_id makes the tree walkable with a recursive CTE.
CREATE TABLE IF NOT EXISTS processes (
    id         INTEGER PRIMARY KEY,
    pod_id     INTEGER NOT NULL REFERENCES pods(id) ON DELETE CASCADE,
    pid        INTEGER NOT NULL CHECK (pid > 0),
    parent_id  INTEGER REFERENCES processes(id) ON DELETE SET NULL,
    exe        TEXT NOT NULL,
    args       TEXT NOT NULL DEFAULT '',
    uid        INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    ended_at   INTEGER,
    CHECK (ended_at IS NULL OR ended_at >= started_at),
    UNIQUE (pod_id, pid, started_at)
);
CREATE INDEX IF NOT EXISTS processes_parent ON processes(parent_id);
-- Partial unique index: at most ONE live process per (pod, pid). Resolving "which process is pid 4242
-- right now?" is an index lookup, and a second live copy is impossible.
CREATE UNIQUE INDEX IF NOT EXISTS processes_live ON processes(pod_id, pid) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    tenant_id  INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,  -- denormalised: tenant-scoped scans skip a join
    pod_id     INTEGER NOT NULL REFERENCES pods(id) ON DELETE CASCADE,
    process_id INTEGER REFERENCES processes(id) ON DELETE SET NULL,
    ts         INTEGER NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('exec', 'file', 'network', 'syscall')),
    detail     TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail))
);
CREATE INDEX IF NOT EXISTS events_tenant_ts ON events(tenant_id, ts);
CREATE INDEX IF NOT EXISTS events_pod_ts    ON events(pod_id, ts);
CREATE INDEX IF NOT EXISTS events_process   ON events(process_id);
-- Expression + partial index: "who talked to 45.33.12.9?" without scanning every file/exec event.
CREATE INDEX IF NOT EXISTS events_dst_ip ON events(json_extract(detail, '$.dst_ip')) WHERE kind = 'network';

-- Evidence must not be edited after the fact. Deletion is allowed only for retention.
CREATE TRIGGER IF NOT EXISTS events_append_only BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

-- Full-text search over what happened (exe, args, paths, IPs), kept in sync by triggers.
-- '.', '-', '_' are word characters (so 45.33.12.9 and kube-proxy stay one token); '/' splits paths.
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(body, tokenize = 'unicode61 tokenchars ''.-_''');
CREATE TRIGGER IF NOT EXISTS events_fts_ins AFTER INSERT ON events
BEGIN
    INSERT INTO events_fts(rowid, body)
    SELECT NEW.id, NEW.kind || ' ' || coalesce(p.exe, '') || ' ' || coalesce(p.args, '') || ' ' ||
           (SELECT group_concat(value, ' ') FROM json_each(NEW.detail))
    FROM (SELECT 1) LEFT JOIN processes p ON p.id = NEW.process_id;
END;
CREATE TRIGGER IF NOT EXISTS events_fts_del AFTER DELETE ON events
BEGIN
    DELETE FROM events_fts WHERE rowid = OLD.id;
END;

-- Detection policies. tenant_id NULL = applies to every tenant. `match` is a JSON rule:
--   {"exe_in": [...], "parent_exe_in": [...], "path_prefix": "...", "op_in": [...], "dst_port_in": [...]}
CREATE TABLE IF NOT EXISTS policies (
    id        INTEGER PRIMARY KEY,
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('exec', 'file', 'network', 'syscall')),
    severity  TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    match     TEXT NOT NULL CHECK (json_valid(match)),
    enabled   INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS policies_name ON policies(coalesce(tenant_id, 0), name);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY,
    tenant_id      INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    policy_id      INTEGER NOT NULL REFERENCES policies(id),
    pod_id         INTEGER NOT NULL REFERENCES pods(id) ON DELETE CASCADE,
    fingerprint    TEXT NOT NULL,
    severity       TEXT NOT NULL,
    first_event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    last_event_id  INTEGER REFERENCES events(id) ON DELETE SET NULL,
    first_seen     INTEGER NOT NULL,
    last_seen      INTEGER NOT NULL,
    hits           INTEGER NOT NULL DEFAULT 1 CHECK (hits >= 1),
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
    updated_by     TEXT NOT NULL DEFAULT 'system',
    CHECK (last_seen >= first_seen)
);
-- Dedup: at most ONE unresolved alert per (tenant, fingerprint). Repeats bump `hits` via UPSERT.
-- Once resolved, the same thing happening again opens a fresh alert (the index no longer covers the old row).
CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_active ON alerts(tenant_id, fingerprint) WHERE status <> 'resolved';
CREATE INDEX IF NOT EXISTS alerts_tenant_status ON alerts(tenant_id, status, severity);

CREATE TABLE IF NOT EXISTS alert_history (
    id          INTEGER PRIMARY KEY,
    alert_id    INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
    from_status TEXT NOT NULL,
    to_status   TEXT NOT NULL,
    at          INTEGER NOT NULL,
    actor       TEXT NOT NULL
);

-- State machine enforced in the database: open -> acknowledged -> resolved, or open -> resolved.
CREATE TRIGGER IF NOT EXISTS alerts_status_guard BEFORE UPDATE OF status ON alerts
WHEN OLD.status <> NEW.status AND NOT (
       (OLD.status = 'open'         AND NEW.status IN ('acknowledged', 'resolved'))
    OR (OLD.status = 'acknowledged' AND NEW.status = 'resolved'))
BEGIN
    SELECT RAISE(ABORT, 'illegal alert status transition');
END;

-- Audit trail written by the database itself, so no code path can change status without a record.
CREATE TRIGGER IF NOT EXISTS alerts_status_audit AFTER UPDATE OF status ON alerts
WHEN OLD.status <> NEW.status
BEGIN
    INSERT INTO alert_history(alert_id, from_status, to_status, at, actor)
    VALUES (NEW.id, OLD.status, NEW.status, unixepoch(), NEW.updated_by);
END;
