"""Demo: semantic search over Kubernetes runtime-security alerts, isolated per tenant.

    python demo.py

Generates ~3,000 synthetic alerts for 6 tenants (one tiny), indexes them, then asks questions the way a
SOC analyst would. Shows the hits, the plan the query planner chose, and how many distance computations
it cost. Then it "crashes" (drops the object without closing) and recovers from the WAL.
"""
import random
import shutil
import time

from tinyvec import Collection, HashingEmbedder

TEMPLATES = [
    ("critical", "exec", "shell /bin/{sh} spawned inside container {app} by {parent}, possible container escape"),
    ("high", "exec", "unexpected binary {bin} executed in pod {app} from writable path /tmp"),
    ("high", "network", "outbound connection from {app} to unknown external IP {ip} on port {port}"),
    ("medium", "file", "write to sensitive file {file} in container {app}"),
    ("critical", "file", "read of service account token {tok} by process {bin} in {app}"),
    ("medium", "syscall", "ptrace syscall attempted by {bin} in pod {app}, possible process injection"),
    ("low", "network", "dns lookup of newly registered domain {dom} from {app}"),
    ("high", "exec", "crypto miner {miner} started in pod {app} with high cpu usage"),
    ("medium", "syscall", "mount syscall blocked for {bin} in privileged container {app}"),
    ("low", "file", "package manager {pm} ran inside running container {app}, image drift detected"),
]
FILL = {
    "sh": ["sh", "bash", "dash"], "app": ["payments-api", "checkout", "auth-svc", "ledger", "nginx-ingress", "reports"],
    "parent": ["java", "node", "python3", "php-fpm"], "bin": ["curl", "wget", "nc", "python3", "busybox", "xmrig"],
    "ip": ["45.33.12.9", "185.220.101.4", "103.21.244.0"], "port": ["4444", "8443", "3333", "6667"],
    "file": ["/etc/shadow", "/etc/passwd", "/root/.ssh/authorized_keys", "/etc/kubernetes/admin.conf"],
    "tok": ["/var/run/secrets/kubernetes.io/serviceaccount/token"], "dom": ["xk3-pay.top", "upd4te-cdn.xyz"],
    "miner": ["xmrig", "minerd", "kdevtmpfsi"], "pm": ["apt-get", "apk", "yum", "pip"],
}
TENANTS = {"acme": 900, "globex": 800, "initech": 600, "umbrella": 450, "hooli": 230, "tinycorp": 20}


def make_alerts(seed=7):
    rng = random.Random(seed)
    t0 = 1_732_000_000
    for tenant, n in TENANTS.items():
        for j in range(n):
            sev, kind, tpl = rng.choice(TEMPLATES)
            fill = {k: rng.choice(v) for k, v in FILL.items()}
            fill["app"] = f"{fill['app']}-{rng.getrandbits(20):05x}"   # real pod names carry a hash
            text = tpl.format(**fill) + f" (ns {rng.choice(['default', 'prod', 'billing', 'web'])}, pid {rng.randint(100, 65000)})"
            yield f"{tenant}-{j}", text, {"tenant": tenant, "severity": sev, "kind": kind,
                                         "cluster": rng.choice(["prod-eu", "prod-us", "staging"]),
                                         "ts": t0 + rng.randint(0, 30 * 86400), "text": text}


def show(res, title):
    print(f"\n> {title}\n  plan: {res.plan}  |  distance computations: {res.dist_evals}")
    for h in res.hits:
        p = h.payload
        print(f"  {h.score:.3f}  [{p['tenant']}/{p['severity']}/{p['cluster']}]  {p['text'][:88]}")


def main():
    path = "data/demo"
    shutil.rmtree(path, ignore_errors=True)
    emb = HashingEmbedder(256)
    col = Collection(path, dim=256, fsync=False)  # fsync off for a fast demo load; tests use fsync=True
    t = time.time()
    for key, text, payload in make_alerts():
        col.upsert(key, emb.embed(text), payload)
    print(f"indexed {len(col)} alerts in {time.time() - t:.1f}s")

    q = emb.embed("someone opened a shell in the payments container")
    show(col.search(q, 5, where={"tenant": "acme"}), "acme: 'someone opened a shell in the payments container'")
    show(col.search(q, 5, where={"tenant": "tinycorp"}), "tinycorp (20 alerts) — same question")
    show(col.search(q, 5, where={"tenant": "tinycorp"}, mode="post"),
         "tinycorp with naive post-filtering (watch it come back short or empty)")
    q2 = emb.embed("pod talking to a suspicious external server")
    show(col.search(q2, 5, where={"tenant": "globex", "severity": {"in": ["high", "critical"]},
                                  "cluster": "prod-eu"}), "globex, high+critical, prod-eu: 'pod talking to a suspicious external server'")

    col.snapshot()
    col.upsert("acme-new", emb.embed("reverse shell nc connected back to 185.220.101.4:4444 from checkout"),
               {"tenant": "acme", "severity": "critical", "kind": "network", "cluster": "prod-us",
                "ts": 1_734_600_000, "text": "reverse shell nc connected back to 185.220.101.4:4444 from checkout"})
    del col  # simulate a crash: no close(), no snapshot of the last write
    col = Collection(path, dim=256)
    print(f"\nrecovered after 'crash': {col.recovery}, {len(col)} alerts")
    q3 = emb.embed("reverse shell")
    for ef in (64, 256):
        show(col.search(q3, 3, where={"tenant": "acme"}, mode="hnsw", ef=ef), f"acme: 'reverse shell', HNSW ef={ef}")
    show(col.search(q3, 3, where={"tenant": "acme"}, mode="exact"), "acme: 'reverse shell', exact (ground truth)")
    print("\nThe newest alert (replayed from the WAL) is an outlier: unlike anything else in the graph.\n"
          "At ef=64 the beam search never reaches it; at ef=256 it does. 'Approximate' is a real trade-off,\n"
          "which is why you measure recall (bench.py) and pair vectors with keyword search in production.")


if __name__ == "__main__":
    main()
