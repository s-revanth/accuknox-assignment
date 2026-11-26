-- Named queries. forensics.py loads each block by its "-- name:" header.

-- name: match_policies
-- The rule engine, as one statement: match the new event against every enabled policy that applies to
-- its tenant, and UPSERT an alert per match. ON CONFLICT targets the *partial* unique index, so repeats of
-- an unresolved alert increment hits; it is a single atomic statement, so concurrent writers can't lose
-- an increment. (The WHERE on the SELECT is required by SQLite's upsert grammar.)
INSERT INTO alerts (tenant_id, policy_id, pod_id, fingerprint, severity,
                    first_event_id, last_event_id, first_seen, last_seen)
SELECT e.tenant_id, p.id, e.pod_id,
       p.id || ':' || e.pod_id || ':' || coalesce(pr.exe, '?'),
       p.severity, e.id, e.id, e.ts, e.ts
FROM events e
JOIN policies p
  ON p.enabled = 1 AND p.kind = e.kind AND (p.tenant_id IS NULL OR p.tenant_id = e.tenant_id)
LEFT JOIN processes pr ON pr.id = e.process_id
LEFT JOIN processes pp ON pp.id = pr.parent_id
WHERE e.id = :event_id
  AND (json_type(p.match, '$.exe_in') IS NULL
       OR pr.exe IN (SELECT value FROM json_each(p.match, '$.exe_in')))
  AND (json_type(p.match, '$.parent_exe_in') IS NULL
       OR pp.exe IN (SELECT value FROM json_each(p.match, '$.parent_exe_in')))
  AND (json_type(p.match, '$.path_prefix') IS NULL
       OR substr(json_extract(e.detail, '$.path'), 1, length(json_extract(p.match, '$.path_prefix')))
          = json_extract(p.match, '$.path_prefix'))
  AND (json_type(p.match, '$.op_in') IS NULL
       OR json_extract(e.detail, '$.op') IN (SELECT value FROM json_each(p.match, '$.op_in')))
  AND (json_type(p.match, '$.dst_port_in') IS NULL
       OR json_extract(e.detail, '$.dst_port') IN (SELECT value FROM json_each(p.match, '$.dst_port_in')))
ON CONFLICT (tenant_id, fingerprint) WHERE status <> 'resolved'
DO UPDATE SET hits          = hits + 1,
              last_seen     = max(last_seen, excluded.last_seen),
              last_event_id = excluded.last_event_id;

-- name: ancestry
-- "How did this process get here?" Walk parent links from a process up to the container's root.
WITH RECURSIVE chain(id, parent_id, pid, exe, args, uid, started_at, depth) AS (
    SELECT id, parent_id, pid, exe, args, uid, started_at, 0 FROM processes WHERE id = :process_id
    UNION ALL
    SELECT p.id, p.parent_id, p.pid, p.exe, p.args, p.uid, p.started_at, c.depth + 1
    FROM processes p JOIN chain c ON p.id = c.parent_id
    WHERE c.depth < 64                                   -- guard against a corrupt cycle
)
SELECT depth, pid, exe, args, uid, started_at FROM chain ORDER BY depth DESC;

-- name: blast_radius
-- "What did it do?" Every descendant of a process, with what each one touched.
WITH RECURSIVE tree(id, depth, path) AS (
    SELECT id, 0, exe FROM processes WHERE id = :process_id
    UNION ALL
    SELECT p.id, t.depth + 1, t.path || ' > ' || p.exe
    FROM processes p JOIN tree t ON p.parent_id = t.id
    WHERE t.depth < 64
)
SELECT t.depth, pr.pid, pr.exe, pr.args,
       count(e.id)                                                     AS events,
       coalesce(sum(e.kind = 'network'), 0)                            AS net,
       coalesce(sum(e.kind = 'file'), 0)                               AS file,
       coalesce(group_concat(DISTINCT json_extract(e.detail, '$.dst_ip') || ':' || json_extract(e.detail, '$.dst_port')), '')
                                                                       AS destinations,
       coalesce(group_concat(DISTINCT json_extract(e.detail, '$.path')), '')  AS paths
FROM tree t
JOIN processes pr ON pr.id = t.id
LEFT JOIN events e ON e.process_id = t.id AND e.kind IN ('network', 'file')
GROUP BY t.id
ORDER BY t.path;

-- name: kill_chain
-- Web-shell pattern: a shell whose parent is a web/app server, followed within :window seconds by an
-- outbound connection to a non-private IP from that shell or ANY of its descendants.
WITH RECURSIVE shells AS (
    SELECT s.id AS shell_id, s.pod_id, s.started_at, s.args, par.exe AS parent_exe
    FROM processes s JOIN processes par ON par.id = s.parent_id
    WHERE s.exe IN ('/bin/sh', '/bin/bash', '/bin/dash')
      AND par.exe IN ('php-fpm', 'node', 'java', 'python3', 'nginx')
),
descendants(shell_id, id) AS (
    SELECT shell_id, shell_id FROM shells
    UNION ALL
    SELECT d.shell_id, p.id FROM processes p JOIN descendants d ON p.parent_id = d.id
)
SELECT t.name AS tenant, pod.name AS pod, sh.parent_exe, sh.args AS shell_cmd,
       pr.exe AS connecting_exe,
       json_extract(e.detail, '$.dst_ip') || ':' || json_extract(e.detail, '$.dst_port') AS dst,
       min(e.ts) - sh.started_at AS first_after_s,
       count(*) AS connections
FROM shells sh
JOIN descendants d ON d.shell_id = sh.shell_id
JOIN events e      ON e.process_id = d.id AND e.kind = 'network'
JOIN processes pr  ON pr.id = d.id
JOIN pods pod      ON pod.id = sh.pod_id
JOIN tenants t     ON t.id = pod.tenant_id
WHERE e.ts BETWEEN sh.started_at AND sh.started_at + :window
  AND json_extract(e.detail, '$.dst_ip') NOT LIKE '10.%'
  AND json_extract(e.detail, '$.dst_ip') NOT LIKE '192.168.%'
  AND json_extract(e.detail, '$.dst_ip') NOT LIKE '127.%'
GROUP BY sh.shell_id, pr.id, dst
ORDER BY tenant, pod, first_after_s;

-- name: bursts
-- Sliding-window rate per pod with a window function: events in the trailing :window seconds,
-- plus the gap since the previous event (LAG). Returns each pod's peak window above :threshold.
WITH rated AS (
    SELECT e.pod_id, e.ts,
           count(*) OVER (PARTITION BY e.pod_id ORDER BY e.ts
                          RANGE BETWEEN :window - 1 PRECEDING AND CURRENT ROW) AS in_window,
           e.ts - lag(e.ts) OVER (PARTITION BY e.pod_id ORDER BY e.ts)         AS gap
    FROM events e WHERE e.tenant_id = :tenant_id
),
peaks AS (
    SELECT pod_id, ts, in_window, gap,
           row_number() OVER (PARTITION BY pod_id ORDER BY in_window DESC, ts) AS rn
    FROM rated
)
SELECT p.name AS pod, pk.in_window AS events_in_window, pk.ts AS window_end,
       (SELECT round(avg(gap), 1) FROM rated r WHERE r.pod_id = pk.pod_id) AS avg_gap_s
FROM peaks pk JOIN pods p ON p.id = pk.pod_id
WHERE pk.rn = 1 AND pk.in_window >= :threshold
ORDER BY pk.in_window DESC;

-- name: top_alerts
-- Top-N per tenant in one pass with ROW_NUMBER.
SELECT tenant, severity, policy, pod, hits, status FROM (
    SELECT t.name AS tenant, a.severity, pol.name AS policy, p.name AS pod, a.hits, a.status,
           row_number() OVER (PARTITION BY a.tenant_id
                              ORDER BY CASE a.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                                                       WHEN 'medium' THEN 2 ELSE 1 END DESC,
                                       a.hits DESC) AS rn
    FROM alerts a
    JOIN tenants t    ON t.id = a.tenant_id
    JOIN policies pol ON pol.id = a.policy_id
    JOIN pods p       ON p.id = a.pod_id
    WHERE a.status <> 'resolved'
) WHERE rn <= :n
ORDER BY tenant, rn;

-- name: search
-- Full-text search, ranked by BM25, scoped to one tenant.
SELECT e.id, e.ts, e.kind, p.name AS pod, round(bm25(events_fts), 2) AS rank,
       snippet(events_fts, 0, '[', ']', '…', 8) AS snippet
FROM events_fts
JOIN events e ON e.id = events_fts.rowid
JOIN pods p   ON p.id = e.pod_id
WHERE events_fts MATCH :q AND e.tenant_id = :tenant_id
ORDER BY rank LIMIT :limit;

-- name: who_contacted
SELECT t.name AS tenant, p.name AS pod, pr.exe, e.ts, json_extract(e.detail, '$.dst_port') AS port
FROM events e
JOIN pods p ON p.id = e.pod_id
JOIN tenants t ON t.id = e.tenant_id
LEFT JOIN processes pr ON pr.id = e.process_id
WHERE e.kind = 'network' AND json_extract(e.detail, '$.dst_ip') = :ip
ORDER BY e.ts;

-- name: retention
-- Delete old evidence in bounded batches, so a big purge never holds the write lock for long.
DELETE FROM events WHERE id IN (SELECT id FROM events WHERE ts < :cutoff ORDER BY id LIMIT :batch);
