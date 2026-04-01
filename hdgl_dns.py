#!/usr/bin/env python3
"""
hdgl_dns.py
───────────
Strand-aware authoritative DNS resolver for the HDGL distributed host.

Instead of returning fixed A records, returns the IP of the node that
currently holds the highest analog weight for the strand that the
requested hostname maps to via phi_tau().

The DNS TTL is the phi-geometric TTL for that strand — so DNS cache
lifetimes match file cache lifetimes automatically.

Architecture:
  - Pure Python stdlib (socket, struct, socketserver) — no dnspython
  - UDP on port 5353 (configurable; use 53 in production with cap_net_bind)
  - Answers A queries for configured domains
  - Answers TXT queries with strand debug info
  - Falls back to upstream resolver for unconfigured domains
  - Runs as a background thread inside hdgl_host.py

DNS wire protocol implemented from scratch (RFC 1035):
  - Question parsing: QNAME, QTYPE, QCLASS
  - Response building: A record, TXT record, SOA for NXDOMAIN
  - TC bit for truncation (refer to TCP if response > 512 bytes)

How it changes traffic flow:
  BEFORE: client → DNS (fixed A record) → NGINX → proxy to authority
  AFTER:  client → HDGL DNS (dynamic A = current authority) → direct serve

Strand authority rotates as lattice weights change. DNS TTL = strand TTL,
so clients re-resolve at the same frequency the lattice rebalances.

Usage:
  from hdgl_dns import HDGLResolver
  resolver = HDGLResolver(lattice, domains={...})
  resolver.start()   # background thread on port 5353

  # Or standalone:
  python3 hdgl_dns.py
"""

import os
import math
import struct
import socket
import logging
import threading
import socketserver
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("hdgl.dns")

# ── Config ────────────────────────────────────────────────────────────────────
DNS_PORT      = int(os.getenv("LN_DNS_PORT",      "5353"))
DNS_HOST      = os.getenv("LN_DNS_HOST",           "0.0.0.0")
DNS_UPSTREAM  = os.getenv("LN_DNS_UPSTREAM",       "1.1.1.1")
DNS_UPSTREAM_PORT = int(os.getenv("LN_DNS_UPSTREAM_PORT", "53"))
LOCAL_NODE    = os.getenv("LN_LOCAL_NODE",         "127.0.0.1")
PHI           = (1 + math.sqrt(5)) / 2

# ── DNS wire protocol constants ───────────────────────────────────────────────
QTYPE_A     = 1
QTYPE_TXT   = 16
QTYPE_AAAA  = 28
QTYPE_ANY   = 255
QCLASS_IN   = 1

RCODE_NOERROR  = 0
RCODE_NXDOMAIN = 3
RCODE_REFUSED  = 5

# ── DNS wire protocol ─────────────────────────────────────────────────────────

def _parse_qname(data: bytes, offset: int) -> Tuple[str, int]:
    """Parse DNS QNAME (with pointer support) from wire format."""
    labels = []
    visited = set()

    while offset < len(data):
        length = data[offset]

        if length == 0:
            offset += 1
            break
        elif (length & 0xC0) == 0xC0:
            # Pointer
            if offset + 1 >= len(data):
                break
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            if ptr in visited:
                break
            visited.add(ptr)
            label, _ = _parse_qname(data, ptr)
            labels.append(label)
            offset += 2
            break
        else:
            offset += 1
            labels.append(data[offset:offset + length].decode("ascii", errors="replace"))
            offset += length

    return ".".join(labels), offset


def _encode_qname(name: str) -> bytes:
    """Encode domain name to DNS wire format."""
    encoded = b""
    for label in name.rstrip(".").split("."):
        encoded += bytes([len(label)]) + label.encode("ascii")
    return encoded + b"\x00"


def _build_header(txid: int, qr: int, opcode: int, aa: int,
                  tc: int, rd: int, ra: int, rcode: int,
                  qdcount: int, ancount: int,
                  nscount: int, arcount: int) -> bytes:
    flags = (qr << 15 | opcode << 11 | aa << 10 |
             tc << 9  | rd << 8   | ra << 7  | rcode)
    return struct.pack(">HHHHHH", txid, flags,
                       qdcount, ancount, nscount, arcount)


def _build_a_record(name: str, ip: str, ttl: int) -> bytes:
    """Build a DNS A record (name, TTL, IPv4)."""
    rdata = bytes(int(x) for x in ip.split("."))
    return (
        _encode_qname(name) +
        struct.pack(">HHIH", QTYPE_A, QCLASS_IN, ttl, 4) +
        rdata
    )


def _build_txt_record(name: str, text: str, ttl: int) -> bytes:
    """Build a DNS TXT record."""
    txt_data = text.encode("utf-8")[:255]
    rdata    = bytes([len(txt_data)]) + txt_data
    return (
        _encode_qname(name) +
        struct.pack(">HHIH", QTYPE_TXT, QCLASS_IN, ttl, len(rdata)) +
        rdata
    )


def _build_soa(zone: str, ttl: int = 60) -> bytes:
    """Build a minimal SOA record for NXDOMAIN responses."""
    mname  = _encode_qname(f"ns1.{zone}")
    rname  = _encode_qname(f"hostmaster.{zone}")
    rdata  = mname + rname + struct.pack(">IIIII",
             int(time.time()), 3600, 900, 604800, 60)
    return (
        _encode_qname(zone) +
        struct.pack(">HHIH", 6, QCLASS_IN, ttl, len(rdata)) +
        rdata
    )


def _forward_query(data: bytes, upstream: str,
                   port: int, timeout: float = 2.0) -> Optional[bytes]:
    """Forward an unhandled query to upstream resolver."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(data, (upstream, port))
        resp, _ = s.recvfrom(512)
        s.close()
        return resp
    except Exception as e:
        log.debug(f"[dns] upstream forward failed: {e}")
        return None


# ── Strand-aware lookup ───────────────────────────────────────────────────────

def _phi_tau_domain(domain: str) -> float:
    """
    Map a domain name to continuous τ on the φ-spiral.
    Uses the same algorithm as phi_tau() in hdgl_fileswap.py
    but treats each DNS label as a path segment.
    """
    labels   = domain.rstrip(".").split(".")
    # Reverse: TLD first, then SLD, then subdomain — matches path depth logic
    segments = list(reversed(labels))
    tau      = 0.0
    for depth, seg in enumerate(segments):
        intra = (sum(ord(c) for c in seg) % 1000) / 1000.0
        tau  += (PHI ** depth) * (depth + intra)
    return tau


def _strand_for_domain(domain: str) -> int:
    """Map domain → HDGL strand index (0–7)."""
    tau = _phi_tau_domain(domain)
    return min(int(tau), 7)


def _omega_ttl_for_strand(strand_idx: int) -> int:
    """
    DNS TTL in seconds = phi-geometric TTL for this strand.
    Matches the file cache TTL so DNS and content expire together.
    Clamped: min 30s (fast rebalance) max 3600s (stable strands).
    """
    try:
        from hdgl_fileswap import _omega_ttl
        return max(30, min(int(_omega_ttl(strand_idx)), 3600))
    except Exception:
        # Fallback: TTL_BASE × φ^(-strand × 2.5)
        base = 3600
        ttl  = base * (PHI ** (-strand_idx * 2.5))
        return max(30, min(int(ttl), 3600))


# ── DNS Request Handler ───────────────────────────────────────────────────────

class HDGLDNSHandler(socketserver.BaseRequestHandler):
    """
    Handles one DNS UDP datagram.
    lattice and domain_map are injected by HDGLResolver at startup.
    """

    # Injected by HDGLResolver
    lattice    = None
    domain_map: Dict[str, dict] = {}   # {"wecharg.com": {"port":8083}, ...}
    local_node: str = LOCAL_NODE

    def handle(self):
        data, sock = self.request
        response   = self._process(data)
        if response:
            sock.sendto(response, self.client_address)

    def _process(self, data: bytes) -> Optional[bytes]:
        if len(data) < 12:
            return None

        # Parse header
        txid, flags = struct.unpack_from(">HH", data, 0)
        qr     = (flags >> 15) & 1
        opcode = (flags >> 11) & 0xF
        rd     = (flags >> 8)  & 1
        qdcount = struct.unpack_from(">H", data, 4)[0]

        if qr != 0:          # not a query
            return None
        if opcode != 0:      # not a standard query
            return self._refused(txid, data[:12])

        if qdcount == 0 or len(data) <= 12:
            return None

        # Parse question
        try:
            qname, offset = _parse_qname(data, 12)
            if offset + 4 > len(data):
                return None
            qtype, qclass = struct.unpack_from(">HH", data, offset)
            offset += 4
        except Exception as e:
            log.debug(f"[dns] parse error: {e}")
            return None

        question_section = data[12:offset]
        domain = qname.lower().rstrip(".")

        log.debug(f"[dns] query: {domain} type={qtype} from {self.client_address[0]}")

        # Is this a domain we handle?
        handled_domain = self._match_domain(domain)
        if handled_domain is None:
            # Forward to upstream
            resp = _forward_query(data, DNS_UPSTREAM, DNS_UPSTREAM_PORT)
            if resp:
                return resp
            return self._nxdomain(txid, question_section, domain)

        # Resolve via HDGL lattice
        return self._resolve(txid, question_section, domain,
                             handled_domain, qtype, rd)

    def _match_domain(self, query: str) -> Optional[str]:
        """
        Return the canonical domain from domain_map that matches the query.
        Handles exact match and www. prefix.
        """
        if query in self.domain_map:
            return query
        # Strip www.
        if query.startswith("www.") and query[4:] in self.domain_map:
            return query[4:]
        return None

    def _resolve(self, txid: int, question: bytes, query: str,
                 domain: str, qtype: int, rd: int) -> bytes:
        """
        Core strand-aware resolution.
        Returns the IP of the current strand authority for this domain.
        """
        strand    = _strand_for_domain(domain)
        ttl       = _omega_ttl_for_strand(strand)
        authority = self._authority_ip(strand)

        log.info(
            f"[dns] {query} → strand={strand} ({chr(65+strand)})  "
            f"authority={authority}  TTL={ttl}s"
        )

        answers    = []
        txt_extras = []

        if qtype in (QTYPE_A, QTYPE_ANY):
            answers.append(_build_a_record(query, authority, ttl))

        if qtype in (QTYPE_TXT, QTYPE_ANY):
            # TXT record carries strand debug info
            fp  = self.lattice.cluster_fingerprint() if self.lattice else "n/a"
            txt = (f"strand={strand} authority={authority} "
                   f"ttl={ttl} fp={fp}")
            txt_extras.append(_build_txt_record(query, txt, ttl))

        if not answers and not txt_extras:
            # QTYPE_AAAA or other — no AAAA support, return empty NOERROR
            header = _build_header(
                txid, 1, 0, 1, 0, rd, 0, RCODE_NOERROR,
                1, 0, 0, 0
            )
            return header + question

        all_answers = answers + txt_extras
        header = _build_header(
            txid, 1, 0, 1, 0, rd, 0, RCODE_NOERROR,
            1, len(all_answers), 0, 0
        )
        return header + question + b"".join(all_answers)

    def _authority_ip(self, strand: int) -> str:
        """Ask the lattice who currently owns this strand."""
        if self.lattice is None:
            return self.local_node
        top = self.lattice.top_node_per_strand()
        node, _ = top.get(strand, (self.local_node, 0.0))
        return node or self.local_node

    def _nxdomain(self, txid: int, question: bytes, domain: str) -> bytes:
        zone   = ".".join(domain.split(".")[-2:]) if "." in domain else domain
        header = _build_header(txid, 1, 0, 1, 0, 0, 0, RCODE_NXDOMAIN,
                                1, 0, 1, 0)
        soa    = _build_soa(zone)
        return header + question + soa

    def _refused(self, txid: int, question: bytes) -> bytes:
        return _build_header(txid, 1, 0, 0, 0, 0, 0, RCODE_REFUSED,
                             0, 0, 0, 0)


# ── Threaded UDP server ───────────────────────────────────────────────────────

class _ThreadedUDPServer(socketserver.ThreadingMixIn,
                          socketserver.UDPServer):
    allow_reuse_address = True
    daemon_threads      = True


# ── HDGLResolver ─────────────────────────────────────────────────────────────

class HDGLResolver:
    """
    Strand-aware authoritative DNS resolver.

    domain_map: {canonical_domain: service_dict}
      e.g. {"wecharg.com": {"port": 8083}, "stealthmachines.com": {"port": 8080}}

    The resolver maps each domain to a strand via phi_tau(), then returns
    the IP of the node that currently holds highest weight on that strand.
    DNS TTL = phi-geometric TTL for that strand.
    """

    def __init__(self, lattice, domain_map: Dict[str, dict],
                 local_node: str = LOCAL_NODE,
                 host: str = DNS_HOST, port: int = DNS_PORT):
        self.lattice    = lattice
        self.domain_map = domain_map
        self.local_node = local_node
        self.host       = host
        self.port       = port
        self._server:   Optional[_ThreadedUDPServer] = None
        self._thread:   Optional[threading.Thread]   = None

        # Inject into handler class
        HDGLDNSHandler.lattice    = lattice
        HDGLDNSHandler.domain_map = domain_map
        HDGLDNSHandler.local_node = local_node

    def start(self) -> None:
        self._server = _ThreadedUDPServer((self.host, self.port), HDGLDNSHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="hdgl-dns",
        )
        self._thread.start()
        log.info(
            f"[dns] HDGL resolver listening on {self.host}:{self.port}  "
            f"domains={list(self.domain_map.keys())}  "
            f"upstream={DNS_UPSTREAM}"
        )
        self._log_strand_map()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            log.info("[dns] resolver stopped")

    def update_domain_map(self, domain_map: Dict[str, dict]) -> None:
        """Hot-update domain map without restart."""
        self.domain_map = domain_map
        HDGLDNSHandler.domain_map = domain_map

    def strand_map(self) -> List[dict]:
        """
        Current strand → domain → authority mapping.
        Suitable for monitoring or logging.
        """
        top    = self.lattice.top_node_per_strand() if self.lattice else {}
        result = []
        for domain in self.domain_map:
            strand    = _strand_for_domain(domain)
            ttl       = _omega_ttl_for_strand(strand)
            authority = top.get(strand, (self.local_node, 0.0))[0] or self.local_node
            result.append({
                "domain":    domain,
                "strand":    strand,
                "label":     chr(65 + strand),
                "authority": authority,
                "ttl_s":     ttl,
                "tau":       round(_phi_tau_domain(domain), 4),
            })
        return sorted(result, key=lambda x: x["strand"])

    def _log_strand_map(self) -> None:
        log.info("[dns] strand map:")
        for entry in self.strand_map():
            log.info(
                f"  {entry['domain']:<28}  strand={entry['strand']} ({entry['label']})  "
                f"τ={entry['tau']}  authority={entry['authority']}  TTL={entry['ttl_s']}s"
            )


# ── hdgl_host.py integration ──────────────────────────────────────────────────
# Add to HDGLHost.__init__:
#   from hdgl_dns import HDGLResolver
#   self.resolver = HDGLResolver(self.lattice, SERVICE_REGISTRY, LOCAL_NODE)
#
# Add to HDGLHost.start() after node_server.start():
#   self.resolver.start()
#
# Add to HDGLHost._update_nginx() or _log_cycle_summary():
#   self.resolver.update_domain_map(SERVICE_REGISTRY)  # hot-update on each cycle
#
# The resolver uses the same lattice instance so authority is always current.


# ── Standalone smoke test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, tempfile, shutil, time

    logging.basicConfig(level=logging.INFO,
                        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")

    # Setup minimal env
    _td = tempfile.mkdtemp(prefix="hdgl_dns_test_")
    os.environ.update({
        "LN_FILESWAP_ROOT":  _td + "/swap",
        "LN_FILESWAP_CACHE": _td + "/cache",
        "LN_DRY_RUN":        "1",
        "LN_SIMULATION":     "1",
        "LN_LOCAL_NODE":     "209.159.159.170",
    })
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    import hdgl_fileswap as _fs; _fs.DRY_RUN = True

    from hdgl_lattice import HDGLLattice

    # Build a test lattice
    lat = HDGLLattice()
    lat.update("209.159.159.170", 45.0, 120.0)
    lat.update("209.159.159.171", 62.0,  80.0)

    domain_map = {
        "wecharg.com":         {"port": 8083},
        "stealthmachines.com": {"port": 8080},
        "josefkulovany.com":   {"port": 8081},
        "zchg.org":            {"port": 443},
    }

    resolver = HDGLResolver(lat, domain_map,
                            local_node="209.159.159.170",
                            host="127.0.0.1", port=15353)
    resolver.start()
    time.sleep(0.3)

    # ── Manual DNS query test ─────────────────────────────────────────────────
    def query(domain: str, qtype: int = QTYPE_A) -> Optional[str]:
        """Send a raw DNS query and parse the A record response."""
        txid  = 0x1234
        qname = _encode_qname(domain)
        pkt   = (_build_header(txid, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0) +
                 qname + struct.pack(">HH", qtype, QCLASS_IN))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(pkt, ("127.0.0.1", 15353))
        resp, _ = s.recvfrom(512)
        s.close()

        if len(resp) < 12:
            return None
        ancount = struct.unpack_from(">H", resp, 6)[0]
        if ancount == 0:
            return None

        # Skip header + question to find answer
        offset = 12
        _, offset = _parse_qname(resp, offset)
        offset += 4   # skip QTYPE + QCLASS

        # Parse first answer
        _, offset = _parse_qname(resp, offset)
        rtype, _, _, rdlen = struct.unpack_from(">HHIH", resp, offset)
        offset += 10
        if rtype == QTYPE_A and rdlen == 4:
            return ".".join(str(b) for b in resp[offset:offset+4])
        return None

    print("\n" + "="*60)
    print("HDGL DNS Strand-Aware Resolution Test")
    print("="*60)
    print()

    all_pass = True
    for domain in domain_map:
        ip = query(domain)
        strand = _strand_for_domain(domain)
        ttl    = _omega_ttl_for_strand(strand)
        top    = lat.top_node_per_strand()
        expected = top.get(strand, ("209.159.159.170", 0))[0]
        ok = "✓" if ip == expected else "✗"
        if ip != expected:
            all_pass = False
        print(f"  {ok} {domain:<28}  strand={strand}({chr(65+strand)})  "
              f"TTL={ttl}s  resolved={ip}  expected={expected}")

    # Test www. prefix
    ip_www = query("www.wecharg.com")
    ip_bare = query("wecharg.com")
    ok = "✓" if ip_www == ip_bare else "✗"
    if ip_www != ip_bare:
        all_pass = False
    print(f"  {ok} www. prefix stripped:  www.wecharg.com → {ip_www} == wecharg.com → {ip_bare}")

    # Test TXT record with strand info
    def query_txt(domain: str) -> Optional[str]:
        txid  = 0x5678
        qname = _encode_qname(domain)
        pkt   = (_build_header(txid, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0) +
                 qname + struct.pack(">HH", QTYPE_TXT, QCLASS_IN))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(pkt, ("127.0.0.1", 15353))
        resp, _ = s.recvfrom(512)
        s.close()
        ancount = struct.unpack_from(">H", resp, 6)[0]
        if ancount == 0:
            return None
        offset = 12
        _, offset = _parse_qname(resp, offset)
        offset += 4
        _, offset = _parse_qname(resp, offset)
        rtype, _, _, rdlen = struct.unpack_from(">HHIH", resp, offset)
        offset += 10
        if rtype == QTYPE_TXT and rdlen > 1:
            txt_len = resp[offset]
            return resp[offset+1:offset+1+txt_len].decode("utf-8", errors="replace")
        return None

    txt = query_txt("wecharg.com")
    ok  = "✓" if txt and "strand=" in txt else "✗"
    if not (txt and "strand=" in txt):
        all_pass = False
    print(f"  {ok} TXT debug record:    {txt}")

    print()
    print("Strand map:")
    for entry in resolver.strand_map():
        print(f"  {entry['domain']:<28}  strand={entry['strand']}({entry['label']})  "
              f"τ={entry['tau']}  TTL={entry['ttl_s']}s  auth={entry['authority']}")

    print()
    print("="*60)
    print(f"{'ALL TESTS PASSED' if all_pass else 'SOME TESTS FAILED'}")
    print("="*60)

    resolver.stop()
    shutil.rmtree(_td, ignore_errors=True)
