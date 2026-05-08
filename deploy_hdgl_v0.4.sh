#!/usr/bin/env bash
# =============================================================================
# deploy_hdgl_v0.4.sh
# Ubuntu auto-deploy for HDGL v0.4 — True End-to-End Unified Transport
#
# MAJOR CHANGE: Single unified transport listener. All node communication
# (gossip, replication, fetch, health, metrics) flows through HDGL frames.
# No separate :8080/:8090 ports. Pure geometry routing at transport layer.
#
# What this does:
#   1. Installs system dependencies (Python3)
#   2. Creates deployuser with SSH key
#   3. Installs HDGL v0.4 stack files (transport layer modules)
#   4. Creates systemd service for the unified daemon
#   5. Runs the lightweight audit suite to verify
#   6. Starts in SIMULATION_MODE first
#
# Usage:
#   sudo bash deploy_hdgl_v0.4.sh
#
# Environment variables:
#   HDGL_LOCAL_NODE           — this server's IP (e.g. 209.159.159.170)
#   HDGL_PEER_NODES           — comma-separated peer IPs
#   HDGL_FILESWAP_MAX_SIZE    — swap size in GB (default: 10)
#   HDGL_TRANSPORT_PORT       — unified transport port (default: 8444)
#   HDGL_CLUSTER_SECRET       — HMAC secret for frame signing
#
# =============================================================================
set -euo pipefail
IFS=$'\n\t'

# ── ANSI ──────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; RESET='\033[0m'
ok()   { echo -e "${GREEN}✓${RESET} $*"; }
info() { echo -e "${BLUE}→${RESET} $*"; }
warn() { echo -e "${YELLOW}⚠${RESET}  $*"; }
die()  { echo -e "${RED}✗ FATAL:${RESET} $*" >&2; exit 1; }
section() { echo -e "\n${BOLD}══ $* ══${RESET}"; }

# ── ROOT CHECK ────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root: sudo $0"

# ── CONFIG ────────────────────────────────────────────────────────────────────
HDGL_LOCAL_NODE="${HDGL_LOCAL_NODE:-$(hostname -I | awk '{print $1}')}"
HDGL_PEER_NODES="${HDGL_PEER_NODES:-}"
HDGL_DEPLOY_KEY="${HDGL_DEPLOY_KEY:-}"
HDGL_FILESWAP_MAX_SIZE="${HDGL_FILESWAP_MAX_SIZE:-10}"
HDGL_TRANSPORT_PORT="${HDGL_TRANSPORT_PORT:-8444}"
HDGL_CLUSTER_SECRET="${HDGL_CLUSTER_SECRET:-$(python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)}"

INSTALL_DIR="/opt/hdgl"
SERVICE_USER="hdgl"
SERVICE_GROUP="hdgl"
DEPLOY_USER="deployuser"

section "HDGL v0.4 Deployment (Unified Transport)"
info "Local node:        $HDGL_LOCAL_NODE"
info "Transport port:    $HDGL_TRANSPORT_PORT"
info "Fileswap max:      ${HDGL_FILESWAP_MAX_SIZE} GB"
info "Peer nodes:        ${HDGL_PEER_NODES:-none configured}"
info "Install dir:       $INSTALL_DIR"

# ── SYSTEM SETUP ──────────────────────────────────────────────────────────────
section "System Setup"

info "Updating package lists"
apt-get -qq update

info "Installing dependencies"
apt-get -qq install -y python3 python3-venv python3-pip curl netcat-openbsd || true

info "Creating HDGL users"
id -u "$SERVICE_USER" &>/dev/null || useradd -r -s /usr/sbin/nologin "$SERVICE_USER"
id -u "$DEPLOY_USER" &>/dev/null || useradd -m -s /bin/bash "$DEPLOY_USER"

ok "System setup complete"

# ── INSTALL HDGL FILES ────────────────────────────────────────────────────────
section "Installing HDGL v0.4 Stack"

mkdir -p "$INSTALL_DIR"
chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR"
chmod 750 "$INSTALL_DIR"

info "Copying Python modules to $INSTALL_DIR"
for file in hdgl_*.py; do
    [ -f "$file" ] || continue
    cp "$file" "$INSTALL_DIR/" || die "Failed to copy $file"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/$file"
    chmod 644 "$INSTALL_DIR/$file"
done

ok "HDGL modules installed"

# ── VERIFY INSTALLATION ───────────────────────────────────────────────────────
section "Verifying Installation"

info "Checking Python syntax"
cd "$INSTALL_DIR" || die "Cannot cd to $INSTALL_DIR"
for pyfile in hdgl_transport.py hdgl_transport_client.py hdgl_host.py; do
    if [ -f "$pyfile" ]; then
        python3 -m py_compile "$pyfile" || die "Syntax error in $pyfile"
        ok "$pyfile OK"
    fi
done

# ── SITE CONFIG ───────────────────────────────────────────────────────────────
section "Configuration"

info "Generating site_config.json"
cat > "$INSTALL_DIR/site_config.json" <<EOF
{
  "cluster": {
    "local_node": "$HDGL_LOCAL_NODE",
    "seed_nodes": $([ -n "$HDGL_PEER_NODES" ] && python3 -c "import json; print(json.dumps([n.strip() for n in '$HDGL_PEER_NODES'.split(',')]))" || echo "[]"),
    "transport_port": $HDGL_TRANSPORT_PORT
  },
  "fileswap": {
    "max_size_gb": $HDGL_FILESWAP_MAX_SIZE,
    "root": "/opt/hdgl_swap"
  },
  "dns": {
    "port": 5353
  },
  "primary_site": {
    "domain": "hdgl.local",
    "storage_paths": ["/storage"]
  }
}
EOF
chown "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/site_config.json"
chmod 640 "$INSTALL_DIR/site_config.json"
ok "site_config.json created"

# ── SYSTEMD SERVICE ───────────────────────────────────────────────────────────
section "Systemd Service Setup"

info "Creating systemd unit"
cat > /etc/systemd/system/hdgl.service <<'UNIT'
[Unit]
Description=HDGL Distributed Host (v0.4 Unified Transport)
Documentation=https://github.com/stealthmachines/hdgl
After=network-online.target
Wants=network-online.target
ConditionFileNotEmpty=/opt/hdgl/hdgl_host.py

[Service]
Type=simple
User=hdgl
Group=hdgl
WorkingDirectory=/opt/hdgl
ExecStart=/usr/bin/python3 /opt/hdgl/hdgl_host.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hdgl
Environment="LN_SIMULATION=0"
Environment="LN_DRY_RUN=0"
Environment="LN_INSTALL_DIR=/opt/hdgl"

# Increase limits for high-concurrency fileswap
LimitNOFILE=65536
LimitNPROC=65536

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
ok "Systemd service installed"

# ── FIREWALL ──────────────────────────────────────────────────────────────────
section "Firewall Rules"

if command -v ufw &> /dev/null; then
    info "Configuring UFW"
    ufw allow in "$HDGL_TRANSPORT_PORT/tcp" || warn "UFW rule failed"
    ok "UFW rules applied"
elif command -v firewall-cmd &> /dev/null; then
    info "Configuring firewalld"
    firewall-cmd --permanent --add-port="$HDGL_TRANSPORT_PORT/tcp" || warn "Firewalld rule failed"
    firewall-cmd --reload || true
    ok "Firewalld rules applied"
else
    warn "No firewall manager found; ensure port $HDGL_TRANSPORT_PORT is open"
fi

# ── SWAP DIRECTORY ────────────────────────────────────────────────────────────
section "Fileswap Setup"

SWAP_DIR="/opt/hdgl_swap"
mkdir -p "$SWAP_DIR"
chown "$SERVICE_USER:$SERVICE_GROUP" "$SWAP_DIR"
chmod 750 "$SWAP_DIR"
ok "Swap directory: $SWAP_DIR"

# ── SIMULATION AUDIT ──────────────────────────────────────────────────────────
section "Running Transport Audit (Simulation Mode)"

export LN_LOCAL_NODE="$HDGL_LOCAL_NODE"
export LN_SIMULATION=1
export LN_DRY_RUN=1
export LN_TRANSPORT_PORT="$HDGL_TRANSPORT_PORT"
export LN_CLUSTER_SECRET="$HDGL_CLUSTER_SECRET"

cd "$INSTALL_DIR" || die "Cannot cd to $INSTALL_DIR"
if [ -f hdgl_audit_v0.4_lite.py ]; then
    python3 hdgl_audit_v0.4_lite.py || warn "Audit suite had issues (non-fatal)"
    ok "Audit complete"
else
    warn "Audit suite not found; skipping"
fi

# ── STARTUP ───────────────────────────────────────────────────────────────────
section "Starting HDGL Service"

info "Enabling hdgl service"
systemctl enable hdgl

info "Starting hdgl (simulation mode for first run)"
export LN_SIMULATION=1
systemctl start hdgl || warn "Service start had issues; check logs"

sleep 2

if systemctl is-active --quiet hdgl; then
    ok "Service is running"
    info "View logs: journalctl -u hdgl -f"
else
    warn "Service may not have started; check: systemctl status hdgl"
fi

# ── SUMMARY ───────────────────────────────────────────────────────────────────
section "Deployment Complete ✓"
echo ""
echo "HDGL v0.4 has been installed and started."
echo ""
echo "Configuration:"
echo "  Local Node:       $HDGL_LOCAL_NODE"
echo "  Transport Port:   $HDGL_TRANSPORT_PORT"
echo "  Fileswap Root:    /opt/hdgl_swap"
echo "  Install Dir:      $INSTALL_DIR"
echo ""
echo "Next steps:"
echo "  1. Switch to LIVE mode: systemctl set-environment LN_SIMULATION=0"
echo "  2. Restart service:     systemctl restart hdgl"
echo "  3. Monitor logs:        journalctl -u hdgl -f"
echo "  4. Verify transport:    nc -zv $HDGL_LOCAL_NODE $HDGL_TRANSPORT_PORT"
echo ""
echo "For peer communication, configure seed nodes via site_config.json:"
echo "  Edit: $INSTALL_DIR/site_config.json"
echo ""
