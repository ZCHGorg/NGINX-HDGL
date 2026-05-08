# HDGL Reset And Redeploy Runbook

This runbook is aligned with the current repository behavior in `deploy_hdgl.sh`,
`hdgl_host.py`, `hdgl_ingress.py`, and `hdgl_node_server.py`.

Use this for a clean redeploy on each node.

---

## 1. Full reset on a node

```bash
# Stop daemon if present
sudo systemctl stop hdgl-daemon 2>/dev/null || true

# Remove HDGL runtime/state directories
sudo rm -rf /opt/hdgl /opt/hdgl_swap /opt/hdgl_cache /var/log/hdgl

# Remove HDGL-managed nginx artifacts
sudo rm -f /etc/nginx/conf.d/living_network.conf
sudo rm -f /etc/nginx/conf.d/living_network.conf.bak
sudo rm -f /etc/nginx/conf.d/hdgl_upstreams.conf

# Validate base nginx still loads
sudo nginx -t && sudo systemctl reload nginx
```

Notes:
- `deploy_hdgl.sh` recreates `/opt/hdgl`, `.env`, service unit, and logrotate.
- Firewall rules are host-policy specific; use your distro policy (UFW or iptables)
  and then verify peer access on TCP 8090.

---

## 2. Upload deployment files

From your workstation:

```bash
scp hdgl_lattice.py hdgl_fileswap.py hdgl_node_server.py hdgl_ingress.py \
  hdgl_host.py hdgl_dns.py hdgl_site_config.py hdgl_moire.py hdgl_netboot.py hdgl_state_db.py \
    hdgl_audit.py hdgl_stability_sim.py hdgl_verify_and_readme.py \
    deploy_hdgl.sh \
    root@NODE_IP:/root/hdgl_deploy/
```

`hdgl_moire_c.so` is optional and can be copied when available.

---

## 3. Run deploy script

On each node:

```bash
cd /root/hdgl_deploy
sudo bash deploy_hdgl.sh
```

Expected deploy behavior:
- installs dependencies
- writes `/opt/hdgl/.env`
- writes `/opt/hdgl/site_config.json`
- auto-generates a cluster secret when one is not supplied
- installs and starts `hdgl-daemon`
- prompts for live-vs-simulation startup mode

---

## 4. Go live on each node

If you chose live mode during deploy, this step is already complete.

If you chose simulation mode, switch later with:

```bash
sudo sed -i 's/LN_SIMULATION=.*/LN_SIMULATION=0/' /opt/hdgl/.env
sudo sed -i 's/LN_DRY_RUN=.*/LN_DRY_RUN=0/' /opt/hdgl/.env
sudo systemctl restart hdgl-daemon
```

For a multi-node cluster, verify all nodes share the same secret:

```bash
grep '^LN_CLUSTER_SECRET=' /opt/hdgl/.env
```

Optional (root-managed cert renew flow):

```bash
grep -q '^LN_CERTBOT_ENABLED=' /opt/hdgl/.env \
  && sudo sed -i 's/LN_CERTBOT_ENABLED=.*/LN_CERTBOT_ENABLED=1/' /opt/hdgl/.env \
  || echo 'LN_CERTBOT_ENABLED=1' | sudo tee -a /opt/hdgl/.env
sudo systemctl restart hdgl-daemon
```

Use this only if your environment gives the daemon a valid certbot execution path. The default generated config leaves `LN_CERTBOT_ENABLED=0` to avoid noisy permission failures.

---

## 5. Verify cluster and routing

```bash
# Health/API
curl -s http://THIS_NODE_IP:8090/health
curl -s http://THIS_NODE_IP:8090/node_info | python3 -m json.tool
curl -s http://THIS_NODE_IP:8090/metrics   | python3 -m json.tool
curl -s http://THIS_NODE_IP:8090/strand_map | python3 -m json.tool

# Logs
tail -f /var/log/hdgl/daemon.log
```

Look for cycle summaries with:
- `peers=...`
- `cluster=...`
- `fp_match=.../32`
- `my_strands=[...]`

---

## 6. Optional controlled failover check

```bash
# On one node, watch authority state
watch -n 3 'curl -s http://THIS_NODE_IP:8090/node_info | python3 -m json.tool'

# On peer node, stop daemon briefly
sudo systemctl stop hdgl-daemon
# then restart
sudo systemctl start hdgl-daemon
```

Authority ownership should rebalance automatically without static role edits.

---

## References

- Primary docs: `README.md`
- Snippet-to-source validation: `COMMAND_VERIFICATION_MATRIX.md`
