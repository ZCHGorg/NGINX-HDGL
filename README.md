# HDGL φ-Spiral — Complete Reset & Deploy

---

## STEP 1 — Full wipe on BOTH nodes

```bash
# Stop everything
systemctl stop hdgl-daemon 2>/dev/null || true

# Remove all HDGL state
rm -rf /opt/hdgl /opt/hdgl_swap /opt/hdgl_cache /var/log/hdgl

# Remove all HDGL nginx config (keep your existing living_network.conf / sites-enabled untouched)
rm -f /etc/nginx/conf.d/living_network.conf
rm -f /etc/nginx/conf.d/living_network.conf.bak
rm -f /etc/nginx/conf.d/hdgl_upstreams.conf

# Remove stale firewall rules
# If using UFW:
ufw delete $(ufw status numbered | grep 8090 | grep -v 'DENY' | awk -F'[][]' '{print $2}' | sort -rn | head -1) 2>/dev/null || true
ufw status numbered | grep 8090   # should show only the DENY rules remaining

# If using iptables — flush all 8090 rules cleanly:
while iptables -D INPUT -p tcp --dport 8090 -j ACCEPT 2>/dev/null; do :; done
while iptables -D INPUT -p tcp --dport 8090 -s <PEER_IP> -j ACCEPT 2>/dev/null; do :; done

# Verify nginx still works
```bash
nginx -t && systemctl reload nginx
echo "CLEAN"
```

#  STEP 2 — Upload files to both nodes
From your local machine:
Bash# Replace with your actual node IPs or hostnames

# Upload to Node A
```bash
scp hdgl_lattice.py hdgl_fileswap.py hdgl_node_server.py hdgl_ingress.py \
    hdgl_host.py hdgl_dns.py hdgl_moire.py hdgl_netboot.py hdgl_state_db.py \
    hdgl_audit.py hdgl_stability_sim.py hdgl_verify_and_readme.py \
    hdgl_moire_c.so deploy_hdgl.sh \
    root@NODE_A_IP:/root/hdgl_deploy/
```

# Upload to Node B
```bash
scp hdgl_lattice.py hdgl_fileswap.py hdgl_node_server.py hdgl_ingress.py \
    hdgl_host.py hdgl_dns.py hdgl_moire.py hdgl_netboot.py hdgl_state_db.py \
    hdgl_audit.py hdgl_stability_sim.py hdgl_verify_and_readme.py \
    hdgl_moire_c.so deploy_hdgl.sh \
    root@NODE_B_IP:/root/hdgl_deploy/
```

# STEP 3 — Deploy Node A (Large node)
Bash# On Node A:
```bash
cd /root/hdgl_deploy
sudo HDGL_LOCAL_NODE=NODE_A_IP \
     HDGL_PEER_NODES=NODE_B_IP \
     bash deploy_hdgl.sh
Expect: ✓ Audit: 57/57 tests passing and HDGL stack deployed successfully.
Then go live:
Bash# Generate cluster secret — use this same value on Node B
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
echo "SECRET: $SECRET"   # SAVE THIS

sed -i "s/LN_CLUSTER_SECRET=.*/LN_CLUSTER_SECRET=$SECRET/" /opt/hdgl/.env
sed -i 's/LN_SIMULATION=1/LN_SIMULATION=0/'                /opt/hdgl/.env
sed -i 's/LN_DRY_RUN=1/LN_DRY_RUN=0/'                     /opt/hdgl/.env
echo "LN_CERTBOT_ENABLED=0"                               >> /opt/hdgl/.env
```

# Verify .env
```bash
grep -E 'SIMULATION|DRY_RUN|SECRET|CERTBOT' /opt/hdgl/.env

systemctl restart hdgl-daemon
sleep 8
tail -15 /var/log/hdgl/daemon.log
Expect: Mode: LIVE, NGINX reloaded, cluster joined, no ERROR lines.
```

# STEP 4 — Deploy Node B (Small node)
Bash# On Node B:
```bash
cd /root/hdgl_deploy
sudo HDGL_LOCAL_NODE=NODE_B_IP \
     HDGL_PEER_NODES=NODE_A_IP \
     bash deploy_hdgl.sh
```

# Set SAME secret as Node A:
```bash
sed -i "s/LN_CLUSTER_SECRET=.*/LN_CLUSTER_SECRET=PASTE_SECRET_HERE/" /opt/hdgl/.env
sed -i 's/LN_SIMULATION=1/LN_SIMULATION=0/'                           /opt/hdgl/.env
sed -i 's/LN_DRY_RUN=1/LN_DRY_RUN=0/'                                /opt/hdgl/.env
echo "LN_CERTBOT_ENABLED=0"                                          >> /opt/hdgl/.env
```

# Add firewall rule for Node A (iptables example):
iptables -I INPUT -p tcp --dport 8090 -s NODE_A_IP -j ACCEPT
iptables-save > /etc/iptables/rules.v4

# Verify — should show exactly ONE rule for Node A:
```bash
iptables -L INPUT -n | grep 8090
```
```bash
systemctl restart hdgl-daemon
sleep 8
tail -15 /var/log/hdgl/daemon.log
Expect: Mode: LIVE, NGINX reloaded, cluster joined — 2 peer(s) healthy.
```

# STEP 5 — Verify it's analog, not fake
Run on Node A once both nodes show clean cycles:
```bash
Bashcd /opt/hdgl && /opt/hdgl/venv/bin/python3 << 'VERIFY'
import sys, json, re, urllib.request
sys.path.insert(0, '/opt/hdgl')
from hdgl_fileswap import _phi_tau, _omega_ttl, STRAND_GEOMETRY

POLYTOPES = ['Point','Line','Triangle','Tetrahedron',
             'Pentachoron','Hexacross','Heptacube','Octacube']

print("=" * 65)
print("ANALOG-OVER-DIGITAL VERIFICATION")
print("=" * 65)

print("\n1. PATH → STRAND  (phi_tau hash, not a lookup table)")
for path in ['/wecharg/','/stealthmachines/','/josefkulovany/',
             '/watt/','/netboot/alpine/kernel']:
    tau    = _phi_tau(path)
    strand = min(int(tau), 7)
    ttl    = _omega_ttl(strand)
    print(f"   {path:<36} tau={tau:.4f}  strand={strand}({chr(65+strand)})  TTL={ttl:.0f}s")

print("\n2. STRAND TTLs  (TTL_BASE x exp(-alpha x SPIRAL_PERIOD))")
for i, ((alpha, *_), poly) in enumerate(zip(STRAND_GEOMETRY, POLYTOPES)):
    ttl  = _omega_ttl(i)
    flag = "STABLE" if alpha < 0 else "volatile"
    print(f"   {chr(65+i)} {poly:<15} alpha={alpha:+.6f}  TTL={ttl:>8.1f}s  {flag}")

print("\n3. LIVE CLUSTER STATE")
for ip in ['NODE_A_IP', 'NODE_B_IP']:
    try:
        d = json.loads(urllib.request.urlopen(
            f'http://{ip}:8090/node_info', timeout=3).read())
        print(f"   {ip}  fp={d['fingerprint']}  excit={d['excitation']:.2f}"
              f"  stor={d['storage_available_gb']:.0f}GB"
              f"  strands={d['authority_strands']}")
    except Exception as e:
        print(f"   {ip}  UNREACHABLE: {e}")

print("\n4. NGINX WEIGHTS  (multiple values = geometry driving nginx)")
try:
    conf    = open('/etc/nginx/conf.d/living_network.conf').read()
    weights = sorted(set(int(w) for w in re.findall(r'weight=(\d+)', conf)))
    print(f"   Weights: {weights}")
    print(f"   {'ANALOG: multiple weights' if len(weights) > 1 else 'single weight — equal latency'}")
except FileNotFoundError:
    print("   living_network.conf not yet written — wait one cycle")

print("\n5. FINGERPRINT DIVERGENCE")
try:
    d1 = json.loads(urllib.request.urlopen(
        f'http://NODE_A_IP:8090/node_info', timeout=3).read())
    d2 = json.loads(urllib.request.urlopen(
        f'http://NODE_B_IP:8090/node_info', timeout=3).read())
    print(f"   Node A: {d1['fingerprint']}  excitation={d1['excitation']:.4f}")
    print(f"   Node B: {d2['fingerprint']}  excitation={d2['excitation']:.4f}")
    if d1['fingerprint'] != d2['fingerprint']:
        print("   ANALOG: different fingerprints — independent computation")
    else:
        print("   converged — identical fingerprints (equal latency)")
except Exception as e:
    print(f"   {e}")

print("=" * 65)
VERIFY
```

# STEP 6 — Adversarial test (the only real proof)
Open two terminals.
Terminal 1 — watch Node B authority continuously:
```bash
Bashwatch -n 3 'curl -s http://NODE_B_IP:8090/node_info | \
  python3 -c "import json,sys; d=json.load(sys.stdin); \
  print(d[\"fingerprint\"], \"strands:\", d[\"authority_strands\"])"'
```

Terminal 2 — kill Node A:
Bash# On Node A:
```bash
systemctl stop hdgl-daemon
Within 60 seconds Node B should take over full authority (strands: [0,1,2,3,4,5,6,7]).
No config was changed. The geometry decided.
```
Bring Node A back:

```bash
systemctl start hdgl-daemon
```

Within another 60 seconds Node B should release authority back to Node A.

Clean success looks like
```bash
textNode A: fp=0xFFFFFFFF  excit=1.00  stor=836GB  strands=[0,1,2,3,4,5,6,7]
Node B: fp=0xFFB00000  excit=0.34  stor=7GB    strands=[]

NGINX weights on Node A: [7, 11, 14, 18, 22, 26, 30, 34]  (8 different values)
NGINX weights on Node B: [1]  (upstream-only config, no server blocks)
```

# living_network.conf: rewritten every 30s by daemon, not by deploy script
The 8 different weight values on the large node are the proof — each strand has a different weight because each polytope has a different alpha value, which produces a different TTL, which produces a different phi-proportional weight. None of those numbers appear anywhere in a config file.
