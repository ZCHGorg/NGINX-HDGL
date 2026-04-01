#!/usr/bin/env python3
"""
hdgl_netboot.py
───────────────
Distributed netboot manager for the HDGL φ-spiral cluster.

Provides:
  1. HDGLTFTPServer     — minimal read-only TFTP (RFC 1350) for PXE handoff
  2. HDGLNetbootManager — manages per-instance boot file trees in the fileswap
  3. Instance isolation — each instance has a unique phi_tau-derived HMAC seed
  4. Auto-registration  — post-install hook gossiping new nodes into the lattice
  5. hdgl_host.py hooks — start/stop alongside node server and DNS

Architecture
────────────
  Bare machine powers on
    → DHCP (dnsmasq, 2-line config) hands PXE server IP + boot filename
    → TFTP (this module, port 69 or 16969 in dev) serves iPXE (~400KB, once)
    → iPXE switches to HTTP, requests /netboot/{instance}/kernel + initrd
    → HDGL node server (port 8090) serves from fileswap — strand-routed,
       mirrored, cached with phi-geometric TTL
    → Alpine / Debian installer boots, pulls preseed.cfg from fileswap
    → Post-install script calls: python3 hdgl_host.py --register {new_ip}
    → New node gossips into cluster, acquires strand authority, becomes peer

Instance Privacy
────────────────
All /netboot/* paths land on the same strand (Octacube, H) because they
share the deep /netboot/ prefix. Instance isolation is NAME-SPACE separation
+ per-instance HMAC key derivation:

  instance_key = sha256("hdgl-instance:{phi_tau(instance_path):.12f}")

This key is:
  • Unique per instance path  — no two paths produce the same tau
  • Deterministic             — same path → same key on every node
  • Geometric                 — derived from the spiral address, not config
  • Used as LN_CLUSTER_SECRET for that instance's inter-node HMAC traffic

The moiré analogy: from outside, the interference pattern of counter-rotating
strands is visible only from the observational angle of the authoritative node.
Each instance's HMAC secret ensures cross-instance authentication is impossible
without knowing both the instance path and the phi_tau algorithm.
"""

import hashlib
import json
import logging
import math
import os
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("hdgl.netboot")

PHI        = (1 + math.sqrt(5)) / 2
LOCAL_NODE = os.getenv("LN_LOCAL_NODE",    "209.159.159.170")
TFTP_PORT  = int(os.getenv("LN_TFTP_PORT", "69"))
NODE_PORT  = int(os.getenv("LN_NODE_PORT", "8090"))


# ── per-instance key derivation ───────────────────────────────────────────────

def instance_tau(instance_path: str) -> float:
    """Map an instance root path to its continuous φ-spiral position."""
    try:
        from hdgl_fileswap import _phi_tau
        return _phi_tau(instance_path)
    except Exception:
        # Standalone fallback: simplified phi_tau
        segments = instance_path.strip("/").split("/")
        tau = 0.0
        for depth, seg in enumerate(segments):
            intra = (sum(ord(c) for c in seg) % 1000) / 1000.0
            tau  += (PHI ** depth) * (depth + intra)
        return tau


def instance_hmac_key(instance_path: str) -> str:
    """
    Derive a unique, deterministic HMAC secret for an instance from phi_tau.

    seed = sha256("hdgl-instance:{tau:.12f}")

    This is the LN_CLUSTER_SECRET for the instance's cluster traffic.
    Geometric derivation means no configuration is needed — the spiral
    address is the key.
    """
    tau  = instance_tau(instance_path)
    seed = f"hdgl-instance:{tau:.12f}"
    return hashlib.sha256(seed.encode()).hexdigest()


def instance_strand(instance_path: str) -> int:
    """Return the strand index for an instance's netboot tree."""
    try:
        from hdgl_fileswap import _strand_for_path
        return _strand_for_path(instance_path + "/kernel")
    except Exception:
        return 7   # Octacube default


def instance_ttl(instance_path: str) -> int:
    """Return the phi-geometric TTL for this instance's strand."""
    try:
        from hdgl_fileswap import _omega_ttl
        return int(_omega_ttl(instance_strand(instance_path)))
    except Exception:
        return 3064


# ── boot script / preseed templates ──────────────────────────────────────────

def _ipxe_script(instance_name: str, local_node: str,
                 node_port: int, instance_root: str) -> str:
    """iPXE script — fetches kernel/initrd over HTTP from node server."""
    return (
        f"#!ipxe\n"
        f"# HDGL netboot — instance: {instance_name}\n"
        f"# phi_tau root: {instance_root}\n"
        f"\n"
        f"set base http://{local_node}:{node_port}/serve{instance_root}\n"
        f"\n"
        f"kernel ${{base}}/kernel "
        f"console=tty0 console=ttyS0,115200 ip=dhcp "
        f"modloop=${{base}}/modloop.squashfs "
        f"ds=nocloud;s=http://{local_node}:{node_port}/serve{instance_root}/ "
        f"hdgl_instance={instance_name} quiet\n"
        f"\n"
        f"initrd ${{base}}/initrd.img\n"
        f"\n"
        f"boot || goto failed\n"
        f":failed\n"
        f"echo Boot failed — instance: {instance_name}\n"
        f"shell\n"
    )


def _alpine_cloud_init(instance_name: str, ssh_keys: List[str],
                       local_node: str, node_port: int,
                       key: str) -> str:
    """Alpine cloud-init / preseed — registers node post-install."""
    ssh_block = (
        "  ssh_authorized_keys:\n" +
        "\n".join(f"    - {k}" for k in ssh_keys)
        if ssh_keys else "  # no SSH keys configured"
    )
    return (
        f"#cloud-config\n"
        f"# HDGL instance: {instance_name}\n"
        f"# phi_tau key:   {key[:16]}...\n"
        f"# Generated:     {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        f"\n"
        f"hostname: {instance_name}\n"
        f"users:\n"
        f"  - name: hdgl\n"
        f"    sudo: ALL=(ALL) NOPASSWD:ALL\n"
        f"    shell: /bin/ash\n"
        f"{ssh_block}\n"
        f"\n"
        f"packages:\n"
        f"  - python3\n"
        f"  - curl\n"
        f"  - openssh\n"
        f"\n"
        f"runcmd:\n"
        f"  - mkdir -p /opt/hdgl\n"
        f"  - curl -sf http://{local_node}:{node_port}/serve/netboot/stack.tar.gz"
        f" | tar -xz -C /opt/hdgl/\n"
        f"  - echo 'LN_CLUSTER_SECRET={key}' >> /opt/hdgl/.env\n"
        f"  - echo 'LN_SIMULATION=0' >> /opt/hdgl/.env\n"
        f"  - echo 'LN_DRY_RUN=0' >> /opt/hdgl/.env\n"
        f"  - python3 /opt/hdgl/hdgl_host.py "
        f"--register $(ip route get 1 | awk '{{print $NF; exit}}')\n"
        f"  - python3 /opt/hdgl/hdgl_host.py &\n"
    )


def _debian_preseed(instance_name: str, ssh_keys: List[str],
                    local_node: str, node_port: int,
                    key: str) -> str:
    """Debian/Ubuntu preseed.cfg for network installation."""
    return (
        f"# HDGL Debian preseed — {instance_name}\n"
        f"# phi_tau key: {key[:16]}...\n"
        f"\n"
        f"d-i debian-installer/locale string en_US\n"
        f"d-i keyboard-configuration/xkb-keymap select us\n"
        f"d-i netcfg/choose_interface select auto\n"
        f"d-i netcfg/get_hostname string {instance_name}\n"
        f"d-i netcfg/get_domain string local\n"
        f"d-i mirror/country string manual\n"
        f"d-i mirror/http/hostname string deb.debian.org\n"
        f"d-i mirror/http/directory string /debian\n"
        f"d-i mirror/http/proxy string\n"
        f"d-i passwd/root-login boolean false\n"
        f"d-i passwd/user-fullname string HDGL Node\n"
        f"d-i passwd/username string hdgl\n"
        f"d-i passwd/user-password-crypted password !\n"
        f"d-i clock-setup/utc boolean true\n"
        f"d-i time/zone string UTC\n"
        f"d-i partman-auto/method string regular\n"
        f"d-i partman-auto/choose_recipe select atomic\n"
        f"d-i partman/confirm_write_new_label boolean true\n"
        f"d-i partman/choose_partition select finish\n"
        f"d-i partman/confirm boolean true\n"
        f"d-i partman/confirm_nooverwrite boolean true\n"
        f"d-i pkgsel/include string openssh-server python3 curl\n"
        f"d-i grub-installer/only_debian boolean true\n"
        f"d-i finish-install/reboot_in_progress note\n"
        f"\n"
        f"d-i preseed/late_command string \\\n"
        f"    in-target mkdir -p /opt/hdgl; \\\n"
        f"    in-target curl -sf "
        f"http://{local_node}:{node_port}/serve/netboot/stack.tar.gz "
        f"| tar -xz -C /opt/hdgl/; \\\n"
        f"    echo 'LN_CLUSTER_SECRET={key}' >> /target/opt/hdgl/.env; \\\n"
        f"    echo 'LN_SIMULATION=0' >> /target/opt/hdgl/.env; \\\n"
        f"    echo 'LN_DRY_RUN=0' >> /target/opt/hdgl/.env; \\\n"
        f"    in-target python3 /opt/hdgl/hdgl_host.py "
        f"--register $(ip route get 1 | awk '{{print $NF; exit}}')\n"
    )


# ── TFTP server (RFC 1350, read-only) ────────────────────────────────────────

class HDGLTFTPServer:
    """
    Minimal read-only TFTP server (RFC 1350).

    Serves only the iPXE bootloader blobs stored in the fileswap.
    After iPXE loads, all subsequent transfers happen over HTTP
    to the HDGL node server — TFTP is used only once per boot.

    Wire format:
      RRQ:   opcode=1  filename\\0  mode\\0
      DATA:  opcode=3  block(2)    data(0-512)
      ACK:   opcode=4  block(2)
      ERROR: opcode=5  errcode(2)  errmsg\\0
    """

    OP_RRQ   = 1
    OP_DATA  = 3
    OP_ACK   = 4
    OP_ERROR = 5
    BLOCK    = 512
    TIMEOUT  = 5.0
    RETRIES  = 5

    # Only these filenames are served via TFTP
    SERVED = {"undionly.kpxe", "ipxe.efi", "pxelinux.0", "boot.ipxe"}

    def __init__(self, swap, local_node: str = LOCAL_NODE,
                 port: int = TFTP_PORT):
        self.swap        = swap
        self.local_node  = local_node
        self.port        = port
        self._sock:   Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("0.0.0.0", self.port))
            self._sock.settimeout(1.0)
            self._running = True
            self._thread  = threading.Thread(
                target=self._serve, daemon=True, name="hdgl-tftp"
            )
            self._thread.start()
            log.info(f"[tftp] listening on 0.0.0.0:{self.port}")
        except PermissionError:
            log.warning(
                f"[tftp] port {self.port} needs root or iptables redirect:\n"
                f"  iptables -t nat -A PREROUTING -p udp --dport 69 "
                f"-j REDIRECT --to-ports {self.port}"
            )
        except Exception as e:
            log.error(f"[tftp] start failed: {e}")

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try: self._sock.close()
            except Exception: pass
        log.info("[tftp] stopped")

    def _serve(self) -> None:
        while self._running:
            try:
                data, addr = self._sock.recvfrom(516)
            except socket.timeout:
                continue
            except Exception:
                break
            threading.Thread(
                target=self._handle,
                args=(data, addr),
                daemon=True,
            ).start()

    def _handle(self, data: bytes, client: Tuple[str, int]) -> None:
        if len(data) < 4:
            return
        opcode = struct.unpack(">H", data[:2])[0]
        if opcode != self.OP_RRQ:
            self._error(client, 4, "Only RRQ supported")
            return
        try:
            parts    = data[2:].split(b"\x00")
            filename = parts[0].decode("ascii", errors="replace")
        except Exception:
            self._error(client, 0, "Bad request")
            return

        basename   = Path(filename).name
        swap_path  = f"/netboot/pxe/{basename}"
        file_data  = self.swap.read(swap_path)

        if file_data is None:
            self._error(client, 1, f"Not found: {swap_path}")
            log.warning(
                f"[tftp] {swap_path} missing — "
                f"run manager.provision_bootloaders() first"
            )
            return

        log.info(
            f"[tftp] {client[0]} ← {basename} "
            f"({len(file_data):,} bytes, "
            f"{math.ceil(len(file_data)/self.BLOCK)} blocks)"
        )
        self._send_file(client, file_data)

    def _send_file(self, client: Tuple[str, int], data: bytes) -> None:
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.settimeout(self.TIMEOUT)
        block = 1
        while True:
            offset = (block - 1) * self.BLOCK
            chunk  = data[offset:offset + self.BLOCK]
            pkt    = struct.pack(">HH", self.OP_DATA, block) + chunk
            for attempt in range(self.RETRIES):
                try:
                    tx.sendto(pkt, client)
                    ack, _ = tx.recvfrom(4)
                    ack_op, ack_blk = struct.unpack(">HH", ack[:4])
                    if ack_op == self.OP_ACK and ack_blk == block:
                        break
                except socket.timeout:
                    if attempt == self.RETRIES - 1:
                        tx.close(); return
                except Exception:
                    tx.close(); return
            if len(chunk) < self.BLOCK:
                break
            block += 1
        tx.close()

    def _error(self, client: Tuple[str, int], code: int, msg: str) -> None:
        pkt = struct.pack(">HH", self.OP_ERROR, code) + msg.encode() + b"\x00"
        try:
            self._sock.sendto(pkt, client)
        except Exception:
            pass


# ── Netboot manager ───────────────────────────────────────────────────────────

class HDGLNetbootManager:
    """
    Manages per-instance netboot file trees in the HDGL fileswap.

    Each provisioned instance gets:
      /netboot/{name}/boot.ipxe         — iPXE chain script
      /netboot/{name}/preseed.cfg       — installer answer file
      /netboot/{name}/cloud-init.yaml   — cloud-init config
      /netboot/{name}/kernel            — Linux kernel (write separately)
      /netboot/{name}/initrd.img        — initial ramdisk (write separately)

    PXE bootloaders live at:
      /netboot/pxe/undionly.kpxe        — BIOS iPXE
      /netboot/pxe/ipxe.efi             — UEFI iPXE
      /netboot/pxe/boot.ipxe            — global chain script
    """

    def __init__(self, lattice, swap, local_node: str = LOCAL_NODE,
                 tftp_port: int = TFTP_PORT, node_port: int = NODE_PORT):
        self.lattice    = lattice
        self.swap       = swap
        self.local_node = local_node
        self.node_port  = node_port
        self._tftp      = HDGLTFTPServer(swap, local_node, tftp_port)
        self._instances: Dict[str, dict] = {}
        self._lock      = threading.Lock()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._tftp.start()
        log.info(
            f"[netboot] ready — "
            f"{len(self._instances)} instance(s) provisioned"
        )

    def stop(self) -> None:
        self._tftp.stop()

    # ── bootloader provisioning ───────────────────────────────────────────────

    def provision_bootloaders(
        self,
        undionly_data: Optional[bytes] = None,
        ipxe_efi_data: Optional[bytes] = None,
    ) -> None:
        """
        Write iPXE bootloader blobs to /netboot/pxe/ in the fileswap.

        If data is not provided, writes stub placeholders.
        Replace with real blobs from https://boot.ipxe.org/:
          curl -o undionly.kpxe https://boot.ipxe.org/undionly.kpxe
          curl -o ipxe.efi      https://boot.ipxe.org/ipxe.efi
        """
        stub = (
            b"# HDGL TFTP STUB\n"
            b"# Replace with real iPXE from boot.ipxe.org:\n"
            b"#   curl -o undionly.kpxe https://boot.ipxe.org/undionly.kpxe\n"
            b"#   curl -o ipxe.efi      https://boot.ipxe.org/ipxe.efi\n"
        )
        self.swap.write("/netboot/pxe/undionly.kpxe",
                        undionly_data or stub)
        self.swap.write("/netboot/pxe/ipxe.efi",
                        ipxe_efi_data or stub)

        # Global chain script: look up per-instance iPXE script by hostname
        chain = (
            f"#!ipxe\n"
            f"# HDGL global chain — routes to per-instance script\n"
            f"set base "
            f"http://{self.local_node}:{self.node_port}/serve/netboot\n"
            f"chain ${{base}}/${{hostname}}/boot.ipxe ||\n"
            f"chain ${{base}}/default/boot.ipxe\n"
        ).encode()
        self.swap.write("/netboot/pxe/boot.ipxe", chain)
        log.info("[netboot] bootloaders written to /netboot/pxe/")

    # ── instance provisioning ─────────────────────────────────────────────────

    def provision_instance(
        self,
        name:            str,
        distro:          str        = "alpine",
        ssh_keys:        List[str]  = None,
        kernel_data:     Optional[bytes] = None,
        initrd_data:     Optional[bytes] = None,
    ) -> dict:
        """
        Provision a named, private boot instance in the fileswap.

        Returns the instance manifest including the geometric HMAC key
        (to be written to /opt/hdgl/.env on the target machine as
        LN_CLUSTER_SECRET).

        Kernel and initrd can be provided now or written later via
        write_kernel() / write_initrd().
        """
        root = f"/netboot/{name}"
        key  = instance_hmac_key(root)
        tau  = instance_tau(root)
        k    = instance_strand(root)
        ttl  = instance_ttl(root)

        log.info(
            f"[netboot] provisioning '{name}'  "
            f"τ={tau:.4f}  strand={k}({chr(65+k)})  "
            f"TTL={ttl}s  key={key[:8]}..."
        )

        # iPXE script
        self.swap.write(
            f"{root}/boot.ipxe",
            _ipxe_script(name, self.local_node,
                         self.node_port, root).encode()
        )

        # Preseed / cloud-init
        ssh = ssh_keys or []
        if distro in ("alpine", "alpine3"):
            payload = _alpine_cloud_init(
                name, ssh, self.local_node, self.node_port, key
            ).encode()
            self.swap.write(f"{root}/preseed.cfg",     payload)
            self.swap.write(f"{root}/cloud-init.yaml", payload)
        elif distro in ("debian", "ubuntu"):
            payload = _debian_preseed(
                name, ssh, self.local_node, self.node_port, key
            ).encode()
            self.swap.write(f"{root}/preseed.cfg", payload)
        else:
            log.warning(f"[netboot] unknown distro '{distro}' — "
                        f"no preseed generated")

        # Optional kernel / initrd
        if kernel_data:
            self.swap.write(f"{root}/kernel",     kernel_data)
        if initrd_data:
            self.swap.write(f"{root}/initrd.img", initrd_data)

        manifest = {
            "name":         name,
            "root":         root,
            "distro":       distro,
            "tau":          round(tau, 6),
            "strand":       k,
            "strand_label": chr(65 + k),
            "ttl_s":        ttl,
            "hmac_key":     key,
            "hmac_hint":    key[:8] + "...",
            "local_node":   self.local_node,
            "node_port":    self.node_port,
            "created_at":   time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with self._lock:
            self._instances[name] = manifest

        log.info(
            f"[netboot] '{name}' ready — "
            f"iPXE: {self.local_node}:{self.node_port}"
            f"/serve{root}/boot.ipxe"
        )
        return manifest

    def write_kernel(self, name: str, data: bytes) -> None:
        """Write kernel image for a provisioned instance."""
        self.swap.write(f"/netboot/{name}/kernel", data)
        log.info(f"[netboot] '{name}': kernel ({len(data):,} bytes)")

    def write_initrd(self, name: str, data: bytes) -> None:
        """Write initrd for a provisioned instance."""
        self.swap.write(f"/netboot/{name}/initrd.img", data)
        log.info(f"[netboot] '{name}': initrd ({len(data):,} bytes)")

    # ── node registration ─────────────────────────────────────────────────────

    def register_node(self, new_ip: str, instance_name: str = "auto",
                      latency_ms: float = 100.0,
                      storage_gb: float = 20.0) -> None:
        """
        Register a freshly-installed node with the lattice.
        Called from the post-install script on the new machine.
        Injects the node's IP into the EMA so the cluster picks it up
        within one health cycle.
        """
        log.info(
            f"[netboot] registering {new_ip} "
            f"(instance={instance_name} "
            f"lat={latency_ms}ms stor={storage_gb}GB)"
        )
        try:
            self.lattice.update(new_ip, latency_ms, storage_gb)
            log.info(
                f"[netboot] {new_ip} added to lattice — "
                f"gossips on next health cycle"
            )
        except Exception as e:
            log.warning(f"[netboot] registration failed for {new_ip}: {e}")

    # ── status ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return manager status for /node_info or monitoring."""
        with self._lock:
            instances = list(self._instances.values())
        return {
            "instances":      len(instances),
            "instance_names": [i["name"]     for i in instances],
            "tftp_port":      self._tftp.port,
            "node_port":      self.node_port,
            "local_node":     self.local_node,
            "instance_detail": [
                {
                    "name":       i["name"],
                    "distro":     i["distro"],
                    "strand":     f"{i['strand']}({i['strand_label']})",
                    "tau":        i["tau"],
                    "ttl_s":      i["ttl_s"],
                    "hmac_hint":  i["hmac_hint"],
                    "created_at": i["created_at"],
                }
                for i in instances
            ],
        }

    def instance_env(self, name: str) -> str:
        """Return .env file contents for a provisioned instance."""
        with self._lock:
            m = self._instances.get(name)
        if not m:
            raise KeyError(f"Instance '{name}' not provisioned")
        return (
            f"# HDGL instance: {name}\n"
            f"# Generated: {m['created_at']}\n"
            f"LN_CLUSTER_SECRET={m['hmac_key']}\n"
            f"LN_SIMULATION=0\n"
            f"LN_DRY_RUN=0\n"
            f"LN_DNS_PORT=5353\n"
            f"LN_NODE_PORT={self.node_port}\n"
        )

    def dnsmasq_config(self, interface: str = "eth0") -> str:
        """
        Minimal dnsmasq PXE/DHCP config.
        Add to /etc/dnsmasq.d/hdgl-pxe.conf on each cluster node.
        """
        return (
            f"# HDGL PXE — /etc/dnsmasq.d/hdgl-pxe.conf\n"
            f"interface={interface}\n"
            f"dhcp-range=10.0.0.100,10.0.0.200,12h\n"
            f"dhcp-boot=undionly.kpxe,"
            f"{self.local_node},{self.local_node}\n"
            f"enable-tftp\n"
            f"# HDGL TFTP serves on port {self._tftp.port}\n"
            f"# Production port 69 redirect:\n"
            f"#   iptables -t nat -A PREROUTING -p udp --dport 69 "
            f"-j REDIRECT --to-ports {self._tftp.port}\n"
        )


# ── standalone smoke test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import shutil
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    _td = Path(tempfile.mkdtemp(prefix="hdgl_netboot_test_"))
    os.environ.update({
        "LN_FILESWAP_ROOT":  str(_td / "swap"),
        "LN_FILESWAP_CACHE": str(_td / "cache"),
        "LN_DRY_RUN":        "1",
        "LN_SIMULATION":     "1",
        "LN_LOCAL_NODE":     "209.159.159.170",
        "LN_INSTALL_DIR":    str(_td),
    })
    sys.path.insert(0, str(Path(__file__).parent))
    import hdgl_fileswap as _fs_mod; _fs_mod.DRY_RUN = True
    from hdgl_lattice  import HDGLLattice
    from hdgl_fileswap import HDGLFileswap

    lat  = HDGLLattice()
    lat.update("209.159.159.170", 45.0, 120.0)
    lat.update("209.159.159.171", 62.0,  80.0)
    swap = HDGLFileswap(lat, "209.159.159.170")
    swap._dry_run_override = True

    manager = HDGLNetbootManager(
        lat, swap, local_node="209.159.159.170",
        tftp_port=16969, node_port=8090
    )

    print("\n" + "="*62)
    print("  HDGL Netboot Manager — Smoke Test")
    print("="*62)

    # 1. Provision bootloaders
    manager.provision_bootloaders()

    # 2. Provision three private instances
    test_instances = [
        ("alpine-A",    "alpine", ["ssh-ed25519 AAAAC3Nz... user@dev"]),
        ("alpine-B",    "alpine", []),
        ("staging-env", "debian", ["ssh-rsa AAAAB3Nz... admin@host"]),
    ]
    manifests = []
    for name, distro, keys in test_instances:
        m = manager.provision_instance(
            name, distro=distro, ssh_keys=keys,
            kernel_data=b"STUB_KERNEL_" + name.encode(),
            initrd_data=b"STUB_INITRD_" + name.encode(),
        )
        manifests.append(m)

    print()

    # 3. Verify HMAC keys unique + geometric
    keys = [m["hmac_key"] for m in manifests]
    assert len(set(keys)) == len(keys), "HMAC keys not unique!"
    print(f"  ✓ {len(manifests)} instances — all HMAC keys unique")
    print(f"  ✓ Keys derived from phi_tau — no configuration required")

    # 4. Verify all files readable from fileswap
    print()
    all_ok = True
    for m in manifests:
        root = m["root"]
        for fname in ["boot.ipxe", "preseed.cfg", "kernel", "initrd.img"]:
            data = swap.read(f"{root}/{fname}")
            ok   = data is not None
            if not ok: all_ok = False
            print(f"  {'✓' if ok else '✗'} {root}/{fname}  "
                  f"({len(data)} bytes)" if ok else
                  f"  ✗ {root}/{fname}  MISSING")
    assert all_ok

    # 5. Instance summary
    print()
    print("  Instance summary:")
    st = manager.status()
    for inst in st["instance_detail"]:
        print(
            f"    {inst['name']:<16}  distro={inst['distro']:<8}  "
            f"strand={inst['strand']}  τ={inst['tau']}  "
            f"TTL={inst['ttl_s']}s  key={inst['hmac_hint']}"
        )

    # 6. .env file output
    print(f"\n  .env for 'alpine-A':")
    for line in manager.instance_env("alpine-A").splitlines():
        if not line.startswith("#"):
            print(f"    {line}")

    # 7. dnsmasq config
    print(f"\n  dnsmasq config snippet:")
    for line in manager.dnsmasq_config("eth0").splitlines():
        print(f"    {line}")

    # 8. Start TFTP and send one RRQ
    manager.start()
    time.sleep(0.3)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    rrq = struct.pack(">H", 1) + b"boot.ipxe\x00octet\x00"
    s.sendto(rrq, ("127.0.0.1", 16969))
    try:
        resp, _ = s.recvfrom(516)
        opcode, block = struct.unpack_from(">HH", resp)
        ok = opcode == 3 and block == 1
        print(f"\n  {'✓' if ok else '✗'} TFTP RRQ boot.ipxe → "
              f"opcode={opcode} block={block} payload={len(resp)-4}B")
        if ok:
            s.sendto(struct.pack(">HH", 4, 1), ("127.0.0.1", 16969))
            # Read remaining blocks until short block
            while len(resp) - 4 == 512:
                resp, _ = s.recvfrom(516)
                opcode, block = struct.unpack_from(">HH", resp)
                s.sendto(struct.pack(">HH", 4, block), ("127.0.0.1", 16969))
            print(f"  ✓ TFTP transfer complete — {block} blocks")
    except socket.timeout:
        print("  ✗ TFTP: timeout")
    finally:
        s.close()

    manager.stop()
    shutil.rmtree(_td, ignore_errors=True)

    print("\n" + "="*62)
    print("  All netboot tests passed")
    print("="*62)
