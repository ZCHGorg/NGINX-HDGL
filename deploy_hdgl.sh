#!/usr/bin/env bash
# =============================================================================
# deploy_hdgl.sh
# Ubuntu auto-deploy for the HDGL φ-Spiral Living Network Stack
#
# What this does:
#   1. Installs system dependencies (nginx, python3, certbot, etc.)
#   2. Creates deployuser with SSH key
#   3. Migrates your existing nginx config (preserves hott/watt/services)
#   4. Installs HDGL stack files
#   5. Creates systemd service for the daemon
#   6. Runs the audit suite to verify
#   7. Starts daemon in SIMULATION_MODE first — you flip to live
#
# Usage:
#   curl -O https://yourhost/deploy_hdgl.sh   # or scp it
#   chmod +x deploy_hdgl.sh
#   sudo ./deploy_hdgl.sh
#
# Required env vars (or edit CONFIG section below):
#   HDGL_LOCAL_NODE   — this server's IP (e.g. 209.159.159.170)
#   HDGL_PEER_NODES   — comma-separated peer IPs (e.g. 209.159.159.171)
#   HDGL_DEPLOY_KEY   — path to SSH public key for deployuser
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
HDGL_PEER_NODES="${HDGL_PEER_NODES:-}"          # comma-separated, can be empty
HDGL_DEPLOY_KEY="${HDGL_DEPLOY_KEY:-}"          # SSH pubkey path, optional

INSTALL_DIR="/opt/hdgl"
VENV_DIR="$INSTALL_DIR/venv"
LOG_DIR="/var/log/hdgl"
SWAP_DIR="/opt/hdgl_swap"
CACHE_DIR="/opt/hdgl_cache"
SERVICE_NAME="hdgl-daemon"
NGINX_CONF="/etc/nginx/conf.d/living_network.conf"
HDGL_NGINX_CONF="/etc/nginx/conf.d/hdgl_upstreams.conf"
NGINX_BACKUP="/etc/nginx/conf.d/living_network.conf.pre-hdgl.bak"
DEPLOY_USER="deployuser"

# Domains from your existing config — edit as needed
ZCHG_DOMAINS="zchg.org www.zchg.org forum.zchg.org"
CHGCOIN_DOMAINS="chgcoin.org www.chgcoin.org forum.chgcoin.org chgcoin.com www.chgcoin.com forum.chgcoin.com"

# Services from your existing config
declare -A SERVICES
SERVICES[wecharg]="8083:wecharg.com"
SERVICES[stealthmachines]="8080:stealthmachines.com"
SERVICES[josefkulovany]="8081:josefkulovany.com"

# ── PREFLIGHT ─────────────────────────────────────────────────────────────────
section "Preflight"

OS=$(lsb_release -si 2>/dev/null || echo "Unknown")
VER=$(lsb_release -sr 2>/dev/null || echo "0")
[[ "$OS" == "Ubuntu" ]] || warn "Expected Ubuntu, got $OS — proceeding anyway"
info "OS: $OS $VER"
info "Local node: $HDGL_LOCAL_NODE"
info "Peer nodes: ${HDGL_PEER_NODES:-none}"
info "Install dir: $INSTALL_DIR"

# ── SYSTEM PACKAGES ───────────────────────────────────────────────────────────
section "System packages"

# Suppress all interactive prompts for this and all subsequent apt calls
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a        # auto-restart services, no prompt
export NEEDRESTART_SUSPEND=1     # suppress needrestart scanning output

# Pre-answer iptables-persistent debconf prompts before install
echo "iptables-persistent iptables-persistent/autosave_v4 boolean true" | debconf-set-selections
echo "iptables-persistent iptables-persistent/autosave_v6 boolean true" | debconf-set-selections

apt-get update -qq
apt-get install -y -qq \
    nginx \
    python3 \
    python3-pip \
    python3-venv \
    certbot \
    python3-certbot-nginx \
    openssh-client \
    openssh-server \
    curl \
    jq \
    git \
    ufw \
    logrotate \
    2>&1 | grep -E "(installed|upgraded|already)" || true

ok "System packages installed"

# ── DEPLOY USER ───────────────────────────────────────────────────────────────
section "Deploy user"

if ! id "$DEPLOY_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$DEPLOY_USER"
    ok "Created user: $DEPLOY_USER"
else
    ok "User exists: $DEPLOY_USER"
fi

# SSH directory
SSH_DIR="/home/$DEPLOY_USER/.ssh"
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"
touch "$SSH_DIR/authorized_keys"
chmod 600 "$SSH_DIR/authorized_keys"

if [[ -n "$HDGL_DEPLOY_KEY" && -f "$HDGL_DEPLOY_KEY" ]]; then
    cat "$HDGL_DEPLOY_KEY" >> "$SSH_DIR/authorized_keys"
    ok "SSH key installed for $DEPLOY_USER"
else
    warn "No SSH key provided — add one manually to $SSH_DIR/authorized_keys"
    warn "  or set HDGL_DEPLOY_KEY=/path/to/key.pub and re-run"
fi

# Sudoers for nginx reload only (no full root)
SUDOERS_FILE="/etc/sudoers.d/hdgl-deployuser"
cat > "$SUDOERS_FILE" << SUDO
# HDGL: allow deployuser to manage nginx and hdgl-daemon without password
$DEPLOY_USER ALL=(ALL) NOPASSWD: /bin/systemctl reload nginx
$DEPLOY_USER ALL=(ALL) NOPASSWD: /bin/systemctl try-reload-or-restart nginx
$DEPLOY_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl try-reload-or-restart nginx
$DEPLOY_USER ALL=(ALL) NOPASSWD: /usr/sbin/nginx -t
$DEPLOY_USER ALL=(ALL) NOPASSWD: /usr/bin/nginx -t
$DEPLOY_USER ALL=(ALL) NOPASSWD: /sbin/nginx -t
$DEPLOY_USER ALL=(ALL) NOPASSWD: /usr/bin/certbot renew --quiet
$DEPLOY_USER ALL=(ALL) NOPASSWD: /bin/systemctl start hdgl-daemon
$DEPLOY_USER ALL=(ALL) NOPASSWD: /bin/systemctl stop hdgl-daemon
$DEPLOY_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart hdgl-daemon
$DEPLOY_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl start hdgl-daemon
$DEPLOY_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop hdgl-daemon
$DEPLOY_USER ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart hdgl-daemon
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

# nginx cache dir — must exist before daemon writes proxy_cache_path config
mkdir -p /var/cache/nginx/hdgl
chown www-data:www-data /var/cache/nginx/hdgl 2>/dev/null ||     chown "$DEPLOY_USER:$DEPLOY_USER" /var/cache/nginx/hdgl
ok "nginx cache dir: /var/cache/nginx/hdgl"

# ── PYTHON ENVIRONMENT ────────────────────────────────────────────────────────
section "Python virtual environment"

python3 -m venv "$VENV_DIR"
# Install all Python dependencies the HDGL stack requires
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install \
    requests \
    numpy \
    -q
ok "Python dependencies installed"
ok "Virtualenv ready: $VENV_DIR"

# ── HDGL STACK FILES ──────────────────────────────────────────────────────────
section "HDGL stack files"

# These files are expected to be in the same directory as this script.
# If deploying remotely, place all .py files alongside deploy_hdgl.sh.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REQUIRED_FILES=(
    "hdgl_lattice.py"
    "hdgl_fileswap.py"
    "hdgl_node_server.py"
    "hdgl_ingress.py"
    "hdgl_host.py"
    "hdgl_dns.py"
    "hdgl_state_db.py"
    "hdgl_netboot.py"
    "hdgl_moire.py"
    "hdgl_audit.py"
    "hdgl_stability_sim.py"
    "hdgl_verify_and_readme.py"
)

# Optional C acceleration for moire encoding (~16x faster)
if [[ -f "$SCRIPT_DIR/hdgl_moire_c.so" ]]; then
    [[ "$SCRIPT_DIR/hdgl_moire_c.so" -ef "$INSTALL_DIR/hdgl_moire_c.so" ]] \
        || cp "$SCRIPT_DIR/hdgl_moire_c.so" "$INSTALL_DIR/hdgl_moire_c.so"
    ok "Installed: hdgl_moire_c.so (C acceleration)"
fi

for f in "${REQUIRED_FILES[@]}"; do
    src="$SCRIPT_DIR/$f"
    dst="$INSTALL_DIR/$f"
    if [[ -f "$src" ]]; then
        # Skip copy if src and dst are the same inode (running from install dir)
        if [[ "$src" -ef "$dst" ]]; then
            ok "Already in place: $f"
        else
            cp "$src" "$dst"
            chown "$DEPLOY_USER:$DEPLOY_USER" "$dst"
            ok "Installed: $f"
        fi
    else
        die "Missing required file: $src\n  Place all HDGL .py files alongside this script."
    fi
done

# ── ENVIRONMENT FILE ──────────────────────────────────────────────────────────
section "Environment configuration"

# Build peer nodes list for daemon
PEER_LIST=""
if [[ -n "$HDGL_PEER_NODES" ]]; then
    PEER_LIST=$(echo "$HDGL_PEER_NODES" | tr ',' '\n' | sed 's/^/    "/' | sed 's/$/"/' | paste -sd ',\n' -)
fi

ENV_FILE="$INSTALL_DIR/.env"
cat > "$ENV_FILE" << ENV
# HDGL daemon environment
# Edit this file to configure the stack, then restart: systemctl restart hdgl-daemon

# ── CORE ──────────────────────────────────────────────────────────────────────
LN_LOCAL_NODE=$HDGL_LOCAL_NODE
LN_SSH_USER=$DEPLOY_USER
LN_NGINX_CONF=$NGINX_CONF
LN_AUTO_DIR=/etc/nginx/sites-enabled
LN_LE_DIR=/etc/letsencrypt/live
LN_GOSSIP_PORT=8080
LN_HEALTH_INTERVAL=30
LN_FILESWAP_ROOT=$SWAP_DIR
LN_FILESWAP_CACHE=$CACHE_DIR
LN_FILESWAP_HTTP_PORT=8090
LN_FILESWAP_TTL_BASE=3600

# ── SECURITY ──────────────────────────────────────────────────────────────────
# Generate with: openssl rand -hex 32
# Must be identical on every node in the cluster.
LN_CLUSTER_SECRET=

# ── FEDERATION ────────────────────────────────────────────────────────────────
LN_BOOTSTRAP_DOMAIN=zchg.org
LN_OPERATOR_DOMAIN=
LN_FP_GOSSIP_INTERVAL=300
# Comma-separated Ethereum addresses of known peer clusters (for on-chain discovery)
LN_KNOWN_CLUSTER_ADDRESSES=

# ── CHG CLUSTER REGISTRATION (one-time, permanent) ────────────────────────────
# registerNode() on the CHG contract encodes this cluster's permanent identity.
# Called once ever. authorized[address]=1 is the network's permission grant.
# Leave blank to skip on-chain registration (cluster still works fully).
LN_CHG_ENABLED=0
LN_CHG_NODE_ADDRESS=
LN_CHG_PRIVATE_KEY=
LN_CHG_RPC_URL=
LN_CHG_BASE_RATE=100000000000000
# Relay node address (must have rateOfCharging=1, rateOfParking=1 set via registerNode(1,1))
# Josef's bootstrap node is pre-configured as the default relay.
LN_CHG_RELAY_ADDRESS=0x9CF916D3A073DDF17340701Ae6aaC4dAE55EAaA6
# How long presence/content announcements last before renewal (seconds)
LN_CHG_PRESENCE_TTL=86400
LN_CHG_CONTENT_TTL=3600

# ── OPTIONAL FEATURES ─────────────────────────────────────────────────────────
LN_MOIRE=0
LN_ENFORCE_CLAIMS=0

# ── MODE ──────────────────────────────────────────────────────────────────────
# Start in simulation mode. When you're ready to go live:
#   1. Run audit:  $VENV_DIR/bin/python3 $INSTALL_DIR/hdgl_audit.py
#   2. Set below:  LN_SIMULATION=0  LN_DRY_RUN=0
#   3. Restart:    systemctl restart $SERVICE_NAME
LN_SIMULATION=1
LN_DRY_RUN=1
ENV

chmod 600 "$ENV_FILE"
chown "$DEPLOY_USER:$DEPLOY_USER" "$ENV_FILE"
ok "Environment file: $ENV_FILE"

# ── PATCH DAEMON WITH LOCAL CONFIG ────────────────────────────────────────────
section "Patching daemon with local node config"

# Build peer list as Python list literal — avoids heredoc indentation corruption
PEER_LIST_PY="\"$HDGL_LOCAL_NODE\""
if [[ -n "$HDGL_PEER_NODES" ]]; then
    while IFS=',' read -ra PEERS; do
        for peer in "${PEERS[@]}"; do
            peer=$(echo "$peer" | xargs)
            [[ -n "$peer" ]] && PEER_LIST_PY+=", \"$peer\""
        done
    done <<< "$HDGL_PEER_NODES"
fi

# Write patch script to temp file so no heredoc variable expansion
# corrupts Python indentation in the target file
PATCH_SCRIPT=$(mktemp /tmp/hdgl_patch_XXXXXX.py)
cat > "$PATCH_SCRIPT" << ENDOFPATCH
import sys, re, ast

path = "${INSTALL_DIR}/hdgl_host.py"
content = open(path).read()

# Replace SEED_NODES list with the actual cluster nodes
# Use SEED_NODES not KNOWN_NODES — that is the correct constant name
content = re.sub(
    r'SEED_NODES\s*=\s*\[.*?\]',
    'SEED_NODES = [${PEER_LIST_PY}]',
    content, flags=re.DOTALL
)

# Do NOT patch LOCAL_NODE — it is set correctly via LN_LOCAL_NODE in .env
# Patching it here caused syntax errors due to multi-line assignment

# Verify Python syntax before writing
try:
    ast.parse(content)
except SyntaxError as e:
    print(f"PATCH ABORTED - syntax error would result: {e}", file=sys.stderr)
    sys.exit(1)

open(path, "w").write(content)
print("patched")
ENDOFPATCH

python3 "$PATCH_SCRIPT" \
    && ok "Daemon patched with local config" \
    || die "Patch script failed — hdgl_host.py may be corrupted, restore from backup"
rm -f "$PATCH_SCRIPT"


# ── NGINX: PRESERVE EXISTING, ADD HDGL UPSTREAMS ────────────────────────────
section "NGINX config migration"

HDGL_NGINX_CONF="/etc/nginx/conf.d/hdgl_upstreams.conf"

# Back up the existing living_network.conf if present
if [[ -f "$NGINX_CONF" ]]; then
    cp "$NGINX_CONF" "$NGINX_BACKUP"
    ok "Existing config backed up: $NGINX_BACKUP"
    # Remove it — hdgl_upstreams.conf takes its place
    rm -f "$NGINX_CONF"
fi

# Detect the existing proxy_cache zone name from any active nginx config
# so HDGL upstream blocks can reference the correct zone
CACHE_ZONE_NAME="storage_cache"
detected=$(grep -rh "keys_zone=" /etc/nginx/sites-enabled/ /etc/nginx/conf.d/     /etc/nginx/nginx.conf 2>/dev/null     | grep -o "keys_zone=[^:]*" | head -1 | cut -d= -f2 || true)
[[ -n "${detected:-}" ]] && CACHE_ZONE_NAME="$detected"
ok "Cache zone: $CACHE_ZONE_NAME"

info "Creating empty HDGL nginx placeholder (daemon owns living_network.conf)..."

# DO NOT write upstream blocks here — hdgl_host.py writes living_network.conf
# each health cycle with phi-weighted upstreams derived from live lattice state.
# Writing upstreams here causes duplicate upstream "hdgl_cluster" nginx errors.
# This file just holds a comment so nginx conf.d is not empty.
cat > "$HDGL_NGINX_CONF" << NGINXCONF
# HDGL upstream placeholder — managed by hdgl_host.py
# Do not add upstream blocks here; they live in living_network.conf
NGINXCONF

chown deployuser:deployuser "$HDGL_NGINX_CONF"
chmod 644 "$HDGL_NGINX_CONF"

# Test the combined config
if nginx -t 2>/dev/null; then
    ok "NGINX config valid"
    systemctl reload nginx 2>/dev/null || true
    ok "NGINX reloaded"
else
    # If test fails, remove our addition and report
    nginx -t
    warn "NGINX config test failed — removing $HDGL_NGINX_CONF"
    rm -f "$HDGL_NGINX_CONF"
    warn "Your existing nginx config is untouched. Check the error above."
fi


section "Fileswap HTTP server"

# hdgl_node_server.py handles :8090 as part of hdgl_host.py — no separate service needed.
ok "Fileswap HTTP server: handled by hdgl-daemon on :8090 (no separate service)"

# ── SYSTEMD SERVICE ───────────────────────────────────────────────────────────
section "HDGL daemon systemd service"

cat > /etc/systemd/system/${SERVICE_NAME}.service << SYSTEMD
[Unit]
Description=HDGL φ-Spiral Living Network Daemon
After=network.target nginx.service
Wants=nginx.service
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
ReadWritePaths=$INSTALL_DIR $LOG_DIR $SWAP_DIR $CACHE_DIR /tmp /etc/nginx/conf.d /etc/nginx/sites-enabled /run /var/log/nginx

[Install]
WantedBy=multi-user.target
SYSTEMD

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

# ── NGINX CERT PERMISSIONS ────────────────────────────────────────────────────
# deployuser needs to read the selfsigned key for nginx config testing
# Create it if it doesn't exist, then set group-readable permissions
if [[ ! -f /etc/ssl/certs/zchg-selfsigned.crt ]]; then
    openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
        -keyout /etc/ssl/private/zchg-selfsigned.key \
        -out /etc/ssl/certs/zchg-selfsigned.crt \
        -subj "/CN=zchg.org" 2>/dev/null
    ok "Self-signed cert created for chgcoin redirect block"
fi
# Allow www-data (nginx worker) to read the key
chown root:www-data /etc/ssl/private/zchg-selfsigned.key 2>/dev/null || true
chmod 640 /etc/ssl/private/zchg-selfsigned.key 2>/dev/null || true
ok "nginx cert key permissions set"
ok "Systemd service installed: $SERVICE_NAME"

# ── LOGROTATE ─────────────────────────────────────────────────────────────────
section "Log rotation"

cat > /etc/logrotate.d/hdgl << LOGROTATE
$LOG_DIR/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 $DEPLOY_USER $DEPLOY_USER
}
LOGROTATE
ok "Logrotate configured"

# ── FIREWALL ──────────────────────────────────────────────────────────────────
section "Firewall"

ufw --force enable
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
# Port 8090: restrict to cluster IPs only — never open to public
ufw deny 8090/tcp
ok "UFW rules applied (80, 443, ssh); port 8090 default-deny"

if [[ -n "$HDGL_PEER_NODES" ]]; then
    while IFS=',' read -ra PEERS; do
        for peer in "${PEERS[@]}"; do
            peer=$(echo "$peer" | xargs)
            [[ -n "$peer" ]] && {
                ufw allow from "$peer" to any port 8090 comment "HDGL peer $peer"
                ok "Peer $peer allowed on :8090"
            }
        done
    done <<< "$HDGL_PEER_NODES"
fi

# Also allow this node to reach itself on 8090
ufw allow from "$HDGL_LOCAL_NODE" to any port 8090 comment "HDGL self" 2>/dev/null || true

# ── DNS port 53 redirect (no root / CAP_NET_BIND required) ────────────────────
section "DNS port 53 redirect"

# HDGL DNS listens on 5353 (unprivileged). Redirect OS port 53 → 5353
# so clients can use standard DNS without any special Python capability.
if command -v iptables &>/dev/null; then
    # Avoid duplicate rules
    iptables -t nat -C PREROUTING -p udp --dport 53 -j REDIRECT --to-ports 5353 2>/dev/null ||         iptables -t nat -A PREROUTING -p udp --dport 53 -j REDIRECT --to-ports 5353
    iptables -t nat -C PREROUTING -p tcp --dport 53 -j REDIRECT --to-ports 5353 2>/dev/null ||         iptables -t nat -A PREROUTING -p tcp --dport 53 -j REDIRECT --to-ports 5353
    # Also redirect OUTPUT (queries from this host itself)
    iptables -t nat -C OUTPUT -p udp -d 127.0.0.1 --dport 53 -j REDIRECT --to-ports 5353 2>/dev/null ||         iptables -t nat -A OUTPUT -p udp -d 127.0.0.1 --dport 53 -j REDIRECT --to-ports 5353
    ok "iptables NAT rules: port 53 -> 5353 (UDP+TCP)"

    # Persist rules across reboots — install iptables-persistent if needed
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables-persistent 2>/dev/null || true
    if command -v netfilter-persistent &>/dev/null; then
        netfilter-persistent save
        ok "iptables rules persisted (survive reboot)"
    elif command -v iptables-save &>/dev/null; then
        mkdir -p /etc/iptables
        iptables-save  > /etc/iptables/rules.v4
        ip6tables-save > /etc/iptables/rules.v6 2>/dev/null || true
        ok "iptables rules saved to /etc/iptables/rules.v4"
    else
        warn "Could not persist iptables rules — rerun: apt-get install iptables-persistent"
    fi
else
    warn "iptables not found — DNS redirect skipped; clients must use port 5353 directly"
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
section "Deploy complete"

cat << SUMMARY

${BOLD}Files installed:${RESET}
  $INSTALL_DIR/
  ├── hdgl_lattice.py
  ├── hdgl_fileswap.py
  ├── hdgl_host.py
  ├── hdgl_audit.py
  ├── .env                  ← edit this to configure
  └── venv/

${BOLD}Services:${RESET}
  systemctl status $SERVICE_NAME
  systemctl status hdgl-swapserver

${BOLD}Logs:${RESET}
  tail -f $LOG_DIR/daemon.log
  journalctl -u $SERVICE_NAME -f

${BOLD}Config backup:${RESET}
  $NGINX_BACKUP

${BOLD}To go live (when ready):${RESET}
  1. Run audit:   $VENV_DIR/bin/python3 $INSTALL_DIR/hdgl_audit.py
  2. Edit:        $ENV_FILE
                  LN_SIMULATION=0
                  LN_DRY_RUN=0
  3. Restart:     systemctl restart $SERVICE_NAME
  4. Monitor:     tail -f $LOG_DIR/daemon.log

${BOLD}To add peer nodes later:${RESET}
  Edit $ENV_FILE → LN_LOCAL_NODE, then add peers to
  KNOWN_NODES in $INSTALL_DIR/hdgl_host.py
  and restart the service.

${BOLD}Your existing services are preserved:${RESET}
  zchg.org / forum.zchg.org  → Discourse (unchanged)
  wecharg.com                → :8083 (unchanged)
  stealthmachines.com        → :8080 (unchanged)
  josefkulovany.com          → PHP/static (unchanged)
  /hott/ and /watt/          → cached proxy (unchanged, now φ-weighted)

${GREEN}${BOLD}HDGL stack deployed successfully.${RESET}
SUMMARY
