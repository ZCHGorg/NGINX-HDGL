# HDGL φ-Spiral Living Network

**Analog-over-digital distributed hosting with phi-weighted load balancing, automatic failover, and self-healing nginx configuration.**

**→ Quick start:** `bash deploy_hdgl.sh` on Ubuntu. Answer prompts. Self-healing distributed cluster ready.

---

## What is it?

HDGL (Hypergeometric Distributed Geometry Layer) is a distributed host stack that replaces traditional static nginx load balancing with a living, self-calibrating system inspired by analog signal theory and phi-spiral geometry.

Where a standard nginx upstream block assigns fixed weights and never adapts, HDGL continuously observes each node's latency, storage availability, and cluster fingerprint — then dynamically regenerates the nginx configuration every 30 seconds to reflect current reality. No human intervention required.

The result is a cluster that routes intelligently, recovers from node failures automatically, and distributes authority across nodes using a mathematical model rooted in the golden ratio (φ ≈ 1.618).

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    HDGL Cluster                          │
│                                                          │
│   Node A (peer-a.example)     Node B (peer-b.example)   │
│   ┌─────────────────────┐     ┌─────────────────────┐   │
│   │  hdgl_host.py       │◄───►│  hdgl_host.py       │   │
│   │  hdgl_lattice.py    │     │  hdgl_lattice.py    │   │
│   │  hdgl_ingress.py    │     │  hdgl_ingress.py    │   │
│   │  hdgl_fileswap.py   │     │  hdgl_fileswap.py   │   │
│   │  nginx (generated)  │     │  nginx (generated)  │   │
│   └─────────────────────┘     └─────────────────────┘   │
│          :8090 gossip ◄──────────► :8090 gossip          │
└─────────────────────────────────────────────────────────┘
```

Each node runs the same binary. There is no master. Authority is determined dynamically by the lattice.

---

## What makes it great

### φ-weighted strand authority

Traffic is not distributed randomly or by round-robin. HDGL divides the cluster into 8 geometric strands (A through H, named after polytopes: Point, Line, Triangle, Tetrahedron, Pentachoron, Hexacross, Heptacube, Octacube). Each strand is weighted using a phi-spiral amplification function:

```python
def _nginx_weight(raw_weight: float) -> int:
    amplified = raw_weight ** 1.2 if raw_weight > 0 else 0.0
    return max(1, min(int(amplified * 20), 100))
```

The `max(1, ...)` floor guarantees every healthy node always receives at least one slot of traffic — no server can be completely starved regardless of how far behind it falls in the EMA scoring.

### Self-healing nginx configuration

Every 30 seconds, HDGL regenerates `/etc/nginx/conf.d/living_network.conf` based on current cluster state and reloads nginx automatically. If you add a node, it appears in the config within one cycle. If a node degrades, its weight drops within cycles. The nginx config is never manually edited — it is always a live reflection of the cluster's analog state.

### Gossip protocol with binary encoding

Nodes announce their health to peers using a compact binary gossip protocol (16 bytes vs 104 bytes for JSON — 83% reduction per gossip POST). Each cycle, a node broadcasts its latency EMA, storage availability, and cluster fingerprint to all healthy peers. Peers update their lattice weights accordingly.

### Omega-TTL caching

Cache TTLs are not fixed values. They are alpha-aware and strand-dependent:

```
TTL_k = TTL_BASE × exp(-alpha_k × SPIRAL_PERIOD)
```

Contracting strands (`alpha < 0`) cache longer, while expanding strands (`alpha > 0`) refresh faster. The result is a self-tuning cache hierarchy tied to lattice geometry, not static per-path rules.

### Analog-over-digital fileswap

Files are not stored on a single server. The `HDGLFileswap` system routes each file path through a phi-tau hash to determine its authoritative strand and node. When strand authority shifts (because node weights changed), files migrate automatically to the new authority. A node that goes offline triggers rebalancing — its files route to the next-best authority within one cycle.

### Cluster fingerprint convergence

Every node computes a 32-bit cluster fingerprint from its lattice state. The cycle log reports `fp_match` as bit distance against the current target mask (`0xFFFF0000` in code), giving a real-time convergence indicator:

```
[cycle 174] peers=2/2  cluster=0xFFFFA5C0  fp_match=29/32  my_strands=['A','B','C','D']
```

---

## Stack components

| File | Role |
|---|---|
| `hdgl_host.py` | Main daemon — health loop, gossip, nginx regeneration |
| `hdgl_lattice.py` | Analog weight engine — EMA, phi-spiral, FOLD256 provisioner |
| `hdgl_ingress.py` | Nginx config generator — upstream blocks, SSL, server blocks |
| `hdgl_fileswap.py` | Strand-addressed file routing, echo, migration |
| `hdgl_node_server.py` | Per-node HTTP server — `/health`, `/node_info`, `/metrics`, `/strand_map`, `/serve/*`, `/gossip`, `/swap_invalidate` |
| `hdgl_dns.py` | DNS resolver with strand-aware TTLs |
| `hdgl_state_db.py` | SQLite persistence for EMA and known nodes |
| `hdgl_audit.py` | Pre-deploy verification suite |

---

## Service registry

Services are no longer hard-coded in the repository. `deploy_hdgl.sh` writes `/opt/hdgl/site_config.json`, and the runtime loads seed peers, primary-domain settings, redirect domains, storage paths, and service definitions from that file via `hdgl_site_config.py`.

This makes the repo publishable as a generic stack: each deployment provides its own topology without editing source files. Proxy services use `name|proxy|domain|port|aliases`. Local PHP/static sites use `name|php_static|domain||aliases|root|php_socket|demo_location|demo_alias`.

---

## Deployment

### Prerequisites

- Ubuntu 24.04 on each node
- Root or passwordless sudo on the target host
- This repository copied to the target host

`deploy_hdgl.sh` handles package installation, virtualenv creation, deployuser creation, systemd unit install, logrotate, firewall setup, DNS port redirect, site config generation, and cluster-secret generation.

### Step 1 — Upload files to each node

```bash
scp hdgl_lattice.py hdgl_fileswap.py hdgl_node_server.py hdgl_ingress.py \
    hdgl_host.py hdgl_dns.py hdgl_site_config.py hdgl_moire.py hdgl_netboot.py hdgl_state_db.py \
    hdgl_audit.py hdgl_stability_sim.py hdgl_verify_and_readme.py \
    deploy_hdgl.sh \
    root@NODE_IP:/root/hdgl_deploy/
```

`hdgl_moire_c.so` is optional and only copied if present.

### Step 2 — Run the deploy script

```bash
ssh root@NODE_IP
cd /root/hdgl_deploy
sudo bash deploy_hdgl.sh
```

The script prompts for local IP, peer IPs, primary domain, redirect domains, storage paths, cluster secret, startup mode, and service definitions. If the cluster secret is left blank, it is generated automatically. For non-interactive installs, provide the same values via environment variables such as `HDGL_LOCAL_NODE`, `HDGL_PEER_NODES`, `HDGL_PRIMARY_DOMAIN`, `HDGL_CLUSTER_SECRET`, `HDGL_START_LIVE`, and `HDGL_SERVICES`.

### Step 3 — Cluster secret and startup mode

```bash
grep '^LN_CLUSTER_SECRET=' /opt/hdgl/.env
grep '^LN_SIMULATION=' /opt/hdgl/.env
grep '^LN_DRY_RUN=' /opt/hdgl/.env
```

If you want multiple nodes to join the same cluster, they must share the same `LN_CLUSTER_SECRET`. You can provide it up front through `HDGL_CLUSTER_SECRET` during deploy, or copy the generated value from the first node into later deployments.

### Step 4 — Verify cluster health

```bash
# From either node:
curl -s http://OTHER_NODE_IP:8090/node_info | python3 -m json.tool

# Watch the logs:
tail -f /var/log/hdgl/daemon.log | grep "cycle\|my_strands\|peers"
```

A healthy cluster looks like:

```
[cycle 12] peers=2/2  cluster=0xFFFFA5C0  fp_match=29/32  my_strands=['A','B','C','D']  energy=1.01e+05
```

---

## Configuration reference

Runtime configuration is split across `/opt/hdgl/.env` and `/opt/hdgl/site_config.json`.

### `.env`

| Variable | Default | Description |
|---|---|---|
| `LN_LOCAL_NODE` | auto-detected | This node's cluster IP (recommended to set explicitly in production) |
| `LN_NODE_PORT` | `8090` | Port for gossip and fileswap HTTP |
| `LN_HEALTH_INTERVAL` | `30` | Seconds between health cycles |
| `LN_NGINX_CONF` | `/etc/nginx/conf.d/living_network.conf` | Generated nginx config path |
| `LN_SITE_CONFIG` | `/opt/hdgl/site_config.json` | Deploy-time domain, peer, and service configuration |
| `LN_LE_DIR` | `/etc/letsencrypt/live` | Let's Encrypt certificate directory |
| `LN_FILESWAP_ROOT` | `/opt/hdgl_swap` | Root directory for swapped files |
| `LN_GOSSIP_PORT` | `8090` | Reserved gossip port setting (current runtime posts gossip over `LN_NODE_PORT`; deploy script may still seed this as `8080`) |
| `LN_FILESWAP_HTTP_PORT` | `8090` | Fileswap HTTP port used for inter-node fetch |
| `LN_FILESWAP_CACHE` | `/opt/hdgl_cache` | Local cache directory for fetched content |
| `LN_FILESWAP_TTL_BASE` | `3600` | Base TTL used by alpha-aware strand TTL model |
| `LN_CLUSTER_SECRET` | generated at deploy if blank | HMAC secret — must match on all nodes |
| `LN_PEER_EVICT_FAILS` | `2` | Failed checks before stale peer eviction |
| `LN_CYCLE_LOG_EVERY` | `4` | Force cycle summary log every N cycles (or on change) |
| `LN_SERVE_FAIL_LOG_EVERY` | `100` | Throttle cadence for repeated serve failure logs |
| `LN_PROXY_TIMEOUT` | `5` | Seconds for authority/mirror proxy request timeout |
| `LN_PASSIVE_SWAP_ENABLE` | `1` | Enable passive mirror capture on remote fetches |
| `LN_PASSIVE_SWAP_MAX_GB` | `7` | Passive mirror disk cap (GB) |
| `LN_PASSIVE_SWAP_TIDY_EVERY` | `64` | Run passive LRU tidy every N passive writes |
| `LN_NGINX_MANAGE_SERVERS` | `0` | `0`: write upstream blocks only when existing server blocks are found |
| `LN_DISCOURSE_SOCK` | `/var/discourse/shared/standalone/nginx.http.sock` | Local Discourse unix socket path |
| `LN_SIMULATION` | `1` | Set to `0` to go live |
| `LN_DRY_RUN` | `0` | Set to `1` for no-write dry-run mode (deploy script initializes this to `1`) |
| `LN_CERTBOT_ENABLED` | `1` | Set to `0` to disable automatic cert renewal |

### `site_config.json`

`deploy_hdgl.sh` generates this file from prompts or environment variables. It contains:

- `seed_nodes`: peer IPs used to bootstrap discovery.
- `primary_site`: canonical domain, aliases, redirect-only domains, storage prefixes, and optional local unix socket for the main app.
- `services`: named service entries with `mode`, `domain`, optional aliases, and either a proxy `port` or PHP/static local-site settings.

### Non-interactive deploy env vars

`deploy_hdgl.sh` accepts these inputs to avoid prompts:

- `HDGL_LOCAL_NODE`
- `HDGL_PEER_NODES`
- `HDGL_DEPLOY_KEY`
- `HDGL_PRIMARY_DOMAIN`
- `HDGL_PRIMARY_ALIASES`
- `HDGL_REDIRECT_DOMAINS`
- `HDGL_STORAGE_PATHS`
- `HDGL_PRIMARY_SOCKET`
- `HDGL_CLUSTER_SECRET`
- `HDGL_START_LIVE`
- `HDGL_SERVICES`

---

## SSL certificates

HDGL generates nginx SSL configuration automatically when Let's Encrypt certificates exist at `$LN_LE_DIR/<domain>/fullchain.pem`. If certificates are absent, the server block falls back to port 80.

The deploy script also creates a self-signed certificate for redirect-only domains when needed. Its common name is derived from your configured primary domain instead of a repo default. Automatic certbot renew is disabled in the generated `.env` by default because the daemon runs as `deployuser`; re-enable it only if you have an operator path that grants certbot the required privileges.

For domains behind Cloudflare where HTTP-01 challenge fails, use DNS-01:

```bash
certbot certonly --manual --preferred-challenges dns -d yourdomain.com
```

---

## Log management

HDGL logs to `/var/log/hdgl/daemon.log`. Log rotation is configured at `/etc/logrotate.d/hdgl`:

```
/var/log/hdgl/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 deployuser deployuser
}
```

Verify logrotate is active on both nodes so daemon logs remain bounded over long uptime.

---

## Troubleshooting

### `peers=X/Y` where Y > number of real nodes

A ghost node (typically `127.0.1.1`) has entered known_nodes via the Ubuntu default `/etc/hosts` mapping `hostname → 127.0.1.1`. Fix on all nodes:

```bash
# Fix /etc/hosts
sed -i 's/127.0.1.1 <hostname>/REAL_IP <hostname>/' /etc/hosts

# Clean the database
python3 -c "
import sqlite3
db = sqlite3.connect('/opt/hdgl/lattice_state.db')
db.execute(\"DELETE FROM known_nodes WHERE node='127.0.1.1'\")
db.execute(\"DELETE FROM lattice_ema WHERE node='127.0.1.1'\")
db.commit()
db.close()
"
systemctl restart hdgl-daemon
```

### `my_strands=none` on one node

One node always loses strand elections because its EMA scores are worse. Reset EMA on both nodes simultaneously:

```bash
python3 -c "
import sqlite3
db = sqlite3.connect('/opt/hdgl/lattice_state.db')
db.execute('DELETE FROM lattice_ema')
db.commit()
db.close()
"
systemctl restart hdgl-daemon
```

### Port 8090 unreachable between nodes

Check UFW rule ordering — a blanket `DENY` rule before specific `ALLOW` rules will block traffic regardless of the allow:

```bash
ufw status numbered   # check ordering
ufw delete deny 8090/tcp   # remove the blanket deny
# specific ALLOW rules for peer IPs remain
```

### nginx config permission errors

The `deployuser` account must own the nginx conf.d directory:

```bash
chown deployuser:deployuser /etc/nginx/conf.d/living_network.conf
chown deployuser:deployuser /etc/nginx/conf.d/living_network.conf.bak
```

---

## Discourse asset failover

HDGL keeps Discourse dynamic traffic on the local unix socket and does not strand-route it. Static asset mirroring under `/opt/hdgl_swap/discourse/public` is an optional operator workflow and is not provided by a built-in sync script in this repository.

When static mirroring is implemented externally, a secondary node can still serve mirrored assets from local storage during origin disruption.

---

## Diagnostic & Control Reference

### Service control

```bash
# Start / stop / restart
sudo systemctl start hdgl-daemon
sudo systemctl stop hdgl-daemon
sudo systemctl restart hdgl-daemon

# Watch live systemd journal (follows)
sudo journalctl -u hdgl-daemon -f

# Status snapshot
sudo systemctl status hdgl-daemon --no-pager
```

### Log inspection

```bash
# Live daemon log
tail -f /var/log/hdgl/daemon.log

# Peer list changes over time
grep "Known peers" /var/log/hdgl/daemon.log | tail -20

# NGINX regeneration events
grep "nginx" /var/log/hdgl/daemon.log | tail -20

# Strand authority shifts
grep "authority" /var/log/hdgl/daemon.log | tail -20

# Any errors
grep -i "error\|exception\|traceback" /var/log/hdgl/daemon.log | tail -30

# Scan for 127.x peer poisoning (should return nothing)
grep "127\." /var/log/hdgl/daemon.log | tail -20

# Scan for path recursion (should return nothing)
grep "/swap/swap/" /var/log/hdgl/daemon.log | tail -20
```

### Live node state (HTTP API)

```bash
# Full node info: health, fingerprint, weights, known_nodes, authority_strands
curl -s http://NODE_A_IP:8090/node_info | python3 -m json.tool
curl -s http://NODE_B_IP:8090/node_info | python3 -m json.tool

# Both nodes side-by-side
for ip in NODE_A_IP NODE_B_IP; do
    echo "=== $ip ==="
    curl -s "http://$ip:8090/node_info" | python3 -m json.tool
done

# Simple liveness probe
curl -s http://NODE_A_IP:8090/health
curl -s http://NODE_B_IP:8090/health

# Per-strand weights + EMA + fingerprint
curl -s http://NODE_A_IP:8090/metrics | python3 -m json.tool

# Current phi-tau routing table (strand -> authority node)
curl -s http://NODE_A_IP:8090/strand_map | python3 -m json.tool

# Fetch a file by logical path (routes automatically to authority)
curl -s "http://NODE_A_IP:8090/serve/your/logical/path"
```

### State DB surgery

```bash
# Inspect state (Python; no sqlite3 binary required)
sudo /opt/hdgl/venv/bin/python3 - <<'PY'
import sqlite3
con = sqlite3.connect("/opt/hdgl/lattice_state.db")
print("--- known_nodes ---")
for r in con.execute("SELECT * FROM known_nodes"):
        print(r)
print("--- lattice_ema (first 10) ---")
for r in con.execute("SELECT * FROM lattice_ema LIMIT 10"):
        print(r)
print("--- metadata ---")
for r in con.execute("SELECT * FROM metadata"):
        print(r)
con.close()
PY

# Emergency clean: remove bad peers from DB (loopback + any specific IP)
sudo /opt/hdgl/venv/bin/python3 - <<'PY'
import sqlite3
con = sqlite3.connect("/opt/hdgl/lattice_state.db")
cur = con.cursor()
cur.execute("DELETE FROM known_nodes WHERE node LIKE '127.%' OR node='0.0.0.0'")
cur.execute("DELETE FROM lattice_ema  WHERE node LIKE '127.%' OR node='0.0.0.0'")
# To evict a specific rogue IP:
# cur.execute("DELETE FROM known_nodes WHERE node='x.x.x.x'")
con.commit()
con.close()
print("done")
PY
```

### Full reset sequence (clean-slate restart on one node)

```bash
sudo systemctl stop hdgl-daemon
sudo pkill -f '/opt/hdgl/hdgl_host.py' || true
sudo pkill -f 'hdgl_node_server.py' || true
sudo rm -f /opt/hdgl_swap/routes.bin /opt/hdgl_swap/routes.json
# Optional full DB wipe (loses EMA history):
# sudo rm -f /opt/hdgl/lattice_state.db
sudo systemctl daemon-reload
sudo systemctl start hdgl-daemon
sleep 10
curl -s http://$(hostname -I | awk '{print $1}'):8090/node_info | python3 -m json.tool
```

### Deploy updated code

```bash
# From staging on either node
sudo install -m 0644 /root/hdgl_deploy/hdgl_host.py        /opt/hdgl/hdgl_host.py
sudo install -m 0644 /root/hdgl_deploy/hdgl_node_server.py /opt/hdgl/hdgl_node_server.py
sudo install -m 0644 /root/hdgl_deploy/hdgl_fileswap.py    /opt/hdgl/hdgl_fileswap.py
sudo install -m 0644 /root/hdgl_deploy/hdgl_lattice.py     /opt/hdgl/hdgl_lattice.py
sudo install -m 0644 /root/hdgl_deploy/hdgl_ingress.py     /opt/hdgl/hdgl_ingress.py
sudo systemctl restart hdgl-daemon
```

### NGINX

```bash
# Verify current NGINX config
sudo nginx -t

# Trigger a fresh generation cycle safely
sudo systemctl restart hdgl-daemon

# View current upstream blocks
grep -A5 "upstream" /etc/nginx/conf.d/living_network.conf 2>/dev/null || \
    cat /etc/nginx/conf.d/hdgl_upstreams.conf 2>/dev/null

# Tail NGINX error log
sudo tail -f /var/log/nginx/error.log
```

### Process and network

```bash
# Confirm port 8090 listener and PID
sudo ss -ltnp 'sport = :8090'

# Confirm who is running it
ps -ef | grep -E 'hdgl_host|hdgl_node' | grep -v grep

# Check environment (what IPs and config the daemon sees)
sudo cat /opt/hdgl/.env
```

---

## What You Built and Why It Matters

HDGL is a masterless distributed host. It is not a static load balancer and not a primary/replica coordinator model. Every node runs identical code; geometry and live measurements decide authority.

### Core mechanism

Each logical path is mapped into a phi-spiral position via `phi_tau`. That position maps to one of 8 strands (A-H). Every strand carries a continuously updated analog weight derived from latency, storage, and Fibonacci-stabilized slot math. The highest-weight node for a strand is the authority for that strand.

No election, no lease, no static shard map. Authority emerges each cycle from shared observations and gossip.

### Why this is unusual

Most distributed hosts use explicit consensus, static ring partitions, or operator-managed role assignment. HDGL converges via analog feedback:

- EMA smooths noisy observations.
- The provisioner pass (`NORM -> SCALE -> PHASESHIFT -> OMEGAMULT -> ENERGY -> FOLD256`) self-calibrates each cycle.
- Nodes with better real conditions naturally gain authority; degraded nodes naturally lose it.

### Phi-spiral addressing

Paths are not treated as flat keys. They map into phi-spiral space, which spreads keys differently than power-of-two hash bucketing. That geometry-driven spread improves strand distribution and lets the authority map adapt continuously.

The Moire layer (`hdgl_moire.py`) overlays deterministic interference encoding using irrational offsets, producing non-periodic but repeatable keystream behavior.

### Practical infrastructure behavior

- NGINX config is regenerated from current lattice state, so traffic weights track real cluster conditions.
- Discourse remains proxied via local unix socket and is intentionally not strand-routed.
- SQLite preserves EMA and peer state across restarts.
- Gossip keeps per-node lattice views in sync without a central coordinator.

### Significance

This is a working proof that a host cluster can run with no designated master and no static routing table while still self-healing and rebalancing based on live physics-like feedback. At two nodes it is already operational; additional nodes increase capacity and routing granularity without redesigning control flow.

---

## Project

HDGL NGINX is part of the CHG (Charg) distributed infrastructure project. The phi-spiral geometry, Omega-TTL caching, and analog-over-digital fileswap are original architectural concepts developed for the CHG network.

Repository: https://github.com/ZCHGorg/NGINX-HDGL

---

## Verification Matrix

For strict command-by-command validation of README snippets against live repo sources, see [COMMAND_VERIFICATION_MATRIX.md](COMMAND_VERIFICATION_MATRIX.md).