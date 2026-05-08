#!/usr/bin/env bash
# =============================================================================
# deploy_hdgl_v0.3.sh
# Ubuntu auto-deploy for HDGL v0.3 — Strand-Native HTTP Server
#
# MAJOR CHANGE: No NGINX. No config files. Pure geometry.
#
# What this does:
#   1. Installs system dependencies (Python3, no nginx)
#   2. Creates deployuser with SSH key
#   3. Installs HDGL v0.3 stack files
#   4. Creates systemd service for the daemon + native HTTP server
#   5. Runs the audit suite to verify
#   6. Starts in SIMULATION_MODE first
#
# Usage:
#   sudo bash deploy_hdgl_v0.3.sh
#
# Environment variables:
#   HDGL_LOCAL_NODE        — this server's IP (e.g. 209.159.159.170)
#   HDGL_PEER_NODES        — comma-separated peer IPs
#   HDGL_FILESWAP_MAX_SIZE — swap size in GB (default: 10)
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

INSTALL_DIR="/opt/hdgl"
VENV_DIR="$INSTALL_DIR/venv"
LOG_DIR="/var/log/hdgl"
SWAP_DIR="/opt/hdgl_swap"
CACHE_DIR="/opt/hdgl_cache"
SERVICE_NAME="hdgl-daemon-v0.3"
DEPLOY_USER="deployuser"

# ── PREFLIGHT ─────────────────────────────────────────────────────────────────
section "Preflight (v0.3: Strand-Native Server)"

OS=$(lsb_release -si 2>/dev/null || echo "Unknown")
VER=$(lsb_release -sr 2>/dev/null || echo "0")
[[ "$OS" == "Ubuntu" ]] || warn "Expected Ubuntu, got $OS — proceeding anyway"
info "OS: $OS $VER"
info "Local node: $HDGL_LOCAL_NODE"
info "Peer nodes: ${HDGL_PEER_NODES:-none}"
info "Version: HDGL v0.3 (strand-native HTTP server, no NGINX)"

# ── SYSTEM PACKAGES (NO NGINX) ────────────────────────────────────────────────
section "System packages (NGINX removed for v0.3)"

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export NEEDRESTART_SUSPEND=1

# Pre-answer iptables prompts
echo "iptables-persistent iptables-persistent/autosave_v4 boolean true" | debconf-set-selections
echo "iptables-persistent iptables-persistent/autosave_v6 boolean true" | debconf-set-selections

apt-get update -qq
apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    openssh-client \
    openssh-server \
    curl \
    jq \
    git \
    ufw \
    logrotate \
    2>&1 | grep -E "(installed|upgraded|already)" || true

ok "System packages installed (NGINX not needed for v0.3)"

# ── DEPLOY USER ───────────────────────────────────────────────────────────────
section "Deploy user"

if ! id "$DEPLOY_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$DEPLOY_USER"
    ok "Created user: $DEPLOY_USER"
else
    ok "User exists: $DEPLOY_USER"
fi

SSH_DIR="/home/$DEPLOY_USER/.ssh"
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
touch "$SSH_DIR/authorized_keys"
chmod 600 "$SSH_DIR/authorized_keys"

if [[ -n "$HDGL_DEPLOY_KEY" && -f "$HDGL_DEPLOY_KEY" ]]; then
    cat "$HDGL_DEPLOY_KEY" >> "$SSH_DIR/authorized_keys"
    ok "SSH key installed for $DEPLOY_USER"
else
    warn "No SSH key provided"
fi

SUDOERS_FILE="/etc/sudoers.d/hdgl-deployuser"
cat > "$SUDOERS_FILE" << SUDO
$DEPLOY_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start hdgl-daemon-v0.3
$DEPLOY_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop hdgl-daemon-v0.3
$DEPLOY_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart hdgl-daemon-v0.3
SUDO
chmod 440 "$SUDOERS_FILE"
ok "Sudoers configured for $DEPLOY_USER"

chown -R "$DEPLOY_USER:$DEPLOY_USER" "$SSH_DIR"

# ── DIRECTORIES ───────────────────────────────────────────────────────────────
section "Directories"

for d in "$INSTALL_DIR" "$LOG_DIR" "$SWAP_DIR" "$CACHE_DIR"; do
    mkdir -p "$d"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$d"
    ok "Created: $d"
done

# ── PYTHON ENVIRONMENT ────────────────────────────────────────────────────────
section "Python virtual environment"

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install \
    aiohttp \
    requests \
    numpy \
    -q
ok "Python dependencies installed (aiohttp for native server)"
ok "Virtualenv ready: $VENV_DIR"

# ── HDGL STACK FILES ──────────────────────────────────────────────────────────
section "HDGL v0.3 stack files"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REQUIRED_FILES=(
    "hdgl_lattice.py"
    "hdgl_fileswap.py"
    "hdgl_node_server.py"
    "hdgl_http_server_native.py"  # NEW: native HTTP server replaces NGINX
    "hdgl_host.py"
    "hdgl_dns.py"
    "hdgl_state_db.py"
    "hdgl_netboot.py"
    "hdgl_moire.py"
    "hdgl_audit.py"
    "hdgl_stability_sim.py"
    "hdgl_verify_and_readme.py"
)

if [[ -f "$SCRIPT_DIR/hdgl_moire_c.so" ]]; then
    cp "$SCRIPT_DIR/hdgl_moire_c.so" "$INSTALL_DIR/hdgl_moire_c.so"
    ok "Installed: hdgl_moire_c.so (C acceleration)"
fi

for f in "${REQUIRED_FILES[@]}"; do
    src="$SCRIPT_DIR/$f"
    dst="$INSTALL_DIR/$f"
    if [[ -f "$src" ]]; then
        if [[ "$src" -ef "$dst" ]]; then
            ok "Already in place: $f"
        else
            cp "$src" "$dst"
            chown "$DEPLOY_USER:$DEPLOY_USER" "$dst"
            ok "Installed: $f"
        fi
    else
        die "Missing required file: $src"
    fi
done

# ── ENVIRONMENT FILE ──────────────────────────────────────────────────────────
section "Environment configuration (v0.3)"

ENV_FILE="$INSTALL_DIR/.env"
cat > "$ENV_FILE" << ENV
# HDGL v0.3 Daemon Environment
# Strand-native server (no NGINX)

# ── CORE ──────────────────────────────────────────────────────────────────────
LN_LOCAL_NODE=$HDGL_LOCAL_NODE
LN_SSH_USER=$DEPLOY_USER
LN_GOSSIP_PORT=8080
LN_HEALTH_INTERVAL=30
LN_HTTP_PORT=8080
LN_HTTPS_PORT=8443
LN_FILESWAP_ROOT=$SWAP_DIR
LN_FILESWAP_CACHE=$CACHE_DIR
LN_FILESWAP_MAX_SIZE=$HDGL_FILESWAP_MAX_SIZE
LN_FILESWAP_TTL_BASE=3600
LN_HTTP_POOL_SIZE=16
LN_HTTP_CONN_TIMEOUT=30.0

# ── SECURITY ──────────────────────────────────────────────────────────────────
LN_CLUSTER_SECRET=

# ── MODE ──────────────────────────────────────────────────────────────────────
LN_SIMULATION=1
LN_DRY_RUN=1
ENV

chmod 600 "$ENV_FILE"
chown "$DEPLOY_USER:$DEPLOY_USER" "$ENV_FILE"
ok "Environment file: $ENV_FILE"

# ── PATCH DAEMON WITH LOCAL CONFIG ────────────────────────────────────────────
section "Patching daemon with local node config"

PEER_LIST_PY="\"$HDGL_LOCAL_NODE\""
if [[ -n "$HDGL_PEER_NODES" ]]; then
    while IFS=',' read -ra PEERS; do
        for peer in "${PEERS[@]}"; do
            peer=$(echo "$peer" | xargs)
            [[ -n "$peer" ]] && PEER_LIST_PY+=", \"$peer\""
        done
    done <<< "$HDGL_PEER_NODES"
fi

PATCH_SCRIPT=$(mktemp /tmp/hdgl_patch_XXXXXX.py)
cat > "$PATCH_SCRIPT" << ENDOFPATCH
import sys, re, ast

path = "${INSTALL_DIR}/hdgl_host.py"
content = open(path).read()

content = re.sub(
    r'SEED_NODES\s*=\s*\[.*?\]',
    'SEED_NODES = [${PEER_LIST_PY}]',
    content, flags=re.DOTALL
)

try:
    ast.parse(content)
except SyntaxError as e:
    print(f"PATCH ABORTED - syntax error: {e}", file=sys.stderr)
    sys.exit(1)

open(path, "w").write(content)
print("patched")
ENDOFPATCH

python3 "$PATCH_SCRIPT" \
    && ok "Daemon patched with local config" \
    || die "Patch script failed"
rm -f "$PATCH_SCRIPT"

# ── SYSTEMD SERVICE (v0.3) ────────────────────────────────────────────────────
section "HDGL v0.3 systemd service (daemon + native HTTP server)"

cat > /etc/systemd/system/${SERVICE_NAME}.service << SYSTEMD
[Unit]
Description=HDGL v0.3 Strand-Native Daemon (no NGINX)
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=$DEPLOY_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV_DIR/bin/python3 $INSTALL_DIR/hdgl_host.py
Restart=on-failure
RestartSec=10

# Logging
StandardOutput=append:$LOG_DIR/daemon.log
StandardError=append:$LOG_DIR/daemon.log

# Hardening
PrivateTmp=yes
ProtectSystem=full
ReadWritePaths=$INSTALL_DIR $LOG_DIR $SWAP_DIR $CACHE_DIR /tmp /var/log

[Install]
WantedBy=multi-user.target
SYSTEMD

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
ok "Systemd service installed: $SERVICE_NAME"

# ── FIREWALL (HTTP ports only, no NGINX) ──────────────────────────────────────
section "Firewall (v0.3: HTTP + HTTPS only)"

ufw --force enable
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
ok "UFW rules applied (80, 443, ssh)"

if [[ -n "$HDGL_PEER_NODES" ]]; then
    while IFS=',' read -ra PEERS; do
        for peer in "${PEERS[@]}"; do
            peer=$(echo "$peer" | xargs)
            [[ -n "$peer" ]] && {
                ufw allow from "$peer" to any port 8080 comment "HDGL peer $peer"
                ok "Peer $peer allowed on :8080"
            }
        done
    done <<< "$HDGL_PEER_NODES"
fi

ufw allow from "$HDGL_LOCAL_NODE" to any port 8080 comment "HDGL self" 2>/dev/null || true

# ── DNS port 53 redirect ──────────────────────────────────────────────────────
section "DNS port 53 redirect"

if command -v iptables &>/dev/null; then
    iptables -t nat -C PREROUTING -p udp --dport 53 -j REDIRECT --to-ports 5353 2>/dev/null || \
        iptables -t nat -A PREROUTING -p udp --dport 53 -j REDIRECT --to-ports 5353
    iptables -t nat -C PREROUTING -p tcp --dport 53 -j REDIRECT --to-ports 5353 2>/dev/null || \
        iptables -t nat -A PREROUTING -p tcp --dport 53 -j REDIRECT --to-ports 5353
    ok "iptables NAT rules: port 53 -> 5353"

    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables-persistent 2>/dev/null || true
    if command -v netfilter-persistent &>/dev/null; then
        netfilter-persistent save
        ok "iptables rules persisted"
    fi
else
    warn "iptables not found — DNS redirect skipped"
fi

# ── AUDIT ─────────────────────────────────────────────────────────────────────
section "Running audit suite"

cd "$INSTALL_DIR"
LN_SIMULATION=1 LN_DRY_RUN=1 \
    "$VENV_DIR/bin/python3" "$INSTALL_DIR/hdgl_audit.py" \
    && ok "Audit: all tests passed" \
    || warn "Audit had failures — review before going live"

# ── START DAEMON (simulation mode) ────────────────────────────────────────────
section "Starting daemon (SIMULATION_MODE=1)"

systemctl start "$SERVICE_NAME"
sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Daemon running in simulation mode"
else
    warn "Daemon failed to start — check: journalctl -u $SERVICE_NAME -n 50"
fi

# ── SUMMARY ───────────────────────────────────────────────────────────────────
section "Deploy complete (HDGL v0.3)"

cat << SUMMARY

${BOLD}HDGL v0.3 — Strand-Native HTTP Server${RESET}
(No NGINX. No config files. Pure geometry.)

${BOLD}Files installed:${RESET}
  $INSTALL_DIR/
  ├── hdgl_host.py
  ├── hdgl_http_server_native.py  ← NEW: handles all HTTP routing
  ├── hdgl_fileswap.py
  ├── hdgl_audit.py
  ├── .env
  └── venv/

${BOLD}Services:${RESET}
  systemctl status $SERVICE_NAME
  systemctl start/stop/restart $SERVICE_NAME

${BOLD}Logs:${RESET}
  tail -f $LOG_DIR/daemon.log
  journalctl -u $SERVICE_NAME -f

${BOLD}Metrics (when live):${RESET}
  curl http://$HDGL_LOCAL_NODE:8080/hdgl/metrics
  curl http://$HDGL_LOCAL_NODE:8080/hdgl/strand-map
  curl http://$HDGL_LOCAL_NODE:8080/hdgl/pool-status

${BOLD}To go live:${RESET}
  1. Run audit:   $VENV_DIR/bin/python3 $INSTALL_DIR/hdgl_audit.py
  2. Edit:        $ENV_FILE
                  LN_SIMULATION=0
                  LN_DRY_RUN=0
  3. Restart:     systemctl restart $SERVICE_NAME
  4. Monitor:     tail -f $LOG_DIR/daemon.log

${BOLD}Routing:${RESET}
  Per-request strand routing via phi_tau(path) → authority
  No NGINX. No weights file. Pure geometry.

${GREEN}${BOLD}HDGL v0.3 deployed successfully.${RESET}
SUMMARY
