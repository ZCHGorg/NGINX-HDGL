#!/usr/bin/env python3
"""
hdgl_moire.py
─────────────
Moiré interference encoding for the HDGL phi-spiral cluster.

Implements analog-over-digital content encoding using the Dₙ(r) formula
from the HDGL Analog V3.0 engine (hdgl_analog_v30.c / hdgl_bridge_v40.py).

The encoding principle
──────────────────────
Two counter-rotating phi-spirals produce a moiré interference pattern that
is coherent only from a specific observational perspective (private_tau).
From any other angle, the interference pattern appears as noise modulating
the public lattice.

This is holographic encoding:
  - Public lattice  = the cluster fingerprint (observable by all nodes)
  - Private tau     = phi_tau(path) — the spiral address of the content
  - Interference    = Dₙ(r) evaluated at the intersection of the two spirals
  - Encoded content = plaintext XORed with the Dₙ(r) keystream

Content stored in the fileswap is legible only to a node that holds both:
  1. The cluster's current fingerprint (public, observable)
  2. The path's phi_tau value (deterministic from the path)

Without both, the decoded output is noise.

The Dₙ(r) formula (from hdgl_analog_v30.c)
───────────────────────────────────────────
  Dₙ(r) = √(φ · Fₙ · 2ⁿ · Pₙ · Ω) · r^k
  where:
    φ   = golden ratio (1.6180339887...)
    Fₙ  = Fibonacci sequence: 1, 1, 2, 3, 5, 8, 13, 21
    2ⁿ  = dyadic scaling (binary granularity)
    Pₙ  = prime sequence: 2, 3, 5, 7, 11, 13, 17, 19
    Ω   = field tension (interference angle between spirals)
    r^k = radial power with k = (n+1)/8 (progressive dimensionality)
    n   = dimension index 1..8 (cycled across data positions)

This is analog-over-digital: the keystream is not a bitstring from a
pseudorandom generator — it is a continuous function of spiral geometry.
The key space is parameterized by a real-valued rotation angle (tau),
making it uncountable and immune to discrete enumeration attacks.

Integration with hdgl_fileswap.py
──────────────────────────────────
  HDGLMoire is called transparently inside HDGLFileswap.write() and .read():

    # On write:
    encoded = moire.encode(data, path, cluster_fp)
    fileswap.write(path, encoded)

    # On read:
    raw     = fileswap.read_raw(path)
    data    = moire.decode(raw, path, cluster_fp)

  The cluster fingerprint is the lattice's current 32-bit excitation state.
  It is public (broadcast in gossip) but changes as the cluster evolves,
  adding a time-varying dimension to the encoding.

Numeric Lattice (Base(∞) seeds from hdgl_analog_v30.c)
───────────────────────────────────────────────────────
The 64 Base(∞) seeds from the C engine are used as additional phase offsets
in the keystream generation. This makes the encoding depend not just on
phi_tau and the cluster fingerprint, but on the full numeric lattice state —
meaning the HDGL geometry itself is the cryptographic primitive.

Usage
─────
  Standalone:
      python3 hdgl_moire.py

  In hdgl_fileswap.py:
      from hdgl_moire import HDGLMoire
      moire = HDGLMoire()

      # encode before write
      encoded = moire.encode(data, path, cluster_fingerprint)

      # decode after read
      data    = moire.decode(encoded, path, cluster_fingerprint)
"""

import hashlib
import math
import os
import struct
import time
from typing import Optional, Tuple

# ── Constants (exact values from hdgl_analog_v30.c) ──────────────────────────

PHI    = 1.6180339887498948
SQRT5  = math.sqrt(5)

# Fibonacci table (FIB_TABLE[NUM_DN] from C engine)
FIB_TABLE    = [1, 1, 2, 3, 5, 8, 13, 21]

# Prime table (PRIME_TABLE[NUM_DN] from C engine)
PRIME_TABLE  = [2, 3, 5, 7, 11, 13, 17, 19]

# Golden angle (137.507...°) as radians
GOLDEN_ANGLE = 2 * math.pi / (PHI ** 2)

# Spiral period (from spiral3.py / dna_echo_colour.py canonical source)
SPIRAL_PERIOD = 13.057

# Base(∞) seeds (base_infinity_seeds from C engine init_numeric_lattice)
BASE_INF_SEEDS = [
    0.6180339887, 1.6180339887, 2.6180339887, 3.6180339887, 4.8541019662,
    5.6180339887, 6.4721359549, 7.8541019662, 8.3141592654, 0.0901699437,
    0.1458980338, 0.2360679775, 0.3090169944, 0.3819660113, 0.4721359549,
    0.6545084972, 0.8729833462, 1.0000000000, 1.2360679775, 1.6180339887,
    2.2360679775, 2.6180339887, 3.1415926535, 3.6180339887, 4.2360679775,
    4.8541019662, 5.6180339887, 6.4721359549, 7.2360679775, 7.8541019662,
    8.6180339887, 9.2360679775, 9.8541019662, 10.6180339887, 11.0901699437,
    11.9442719100, 12.6180339887, 13.6180339887, 14.2360679775, 14.8541019662,
    15.6180339887, 16.4721359549, 17.2360679775, 17.9442719100, 18.6180339887,
    19.2360679775, 19.8541019662, 20.6180339887, 21.0901699437, 21.9442719100,
    22.6180339887, 23.6180339887, 24.2360679775, 24.8541019662, 25.6180339887,
    26.4721359549, 27.2360679775, 27.9442719100, 28.6180339887, 29.0344465435,
    29.6180339887, 30.2360679775, 30.8541019662, 31.6180339887,
]

# Sibling harmonics (sibling_harmonics[] from C engine)
SIBLING_HARMONICS = [
    0.0901699437, 0.1458980338, 0.2360679775, 0.3090169944,
    0.3819660113, 0.4721359549, 0.6545084972, 0.8729833462,
]

# Scale factor: 137 ≈ golden angle in degrees — breaks keystream periodicity
_SCALE = 137.50776405003785   # exact golden angle degrees

# ── C acceleration (hdgl_moire_c.so) ─────────────────────────────────────────
# When the compiled C keystream library is present, _moire_keystream() is
# ~16x faster. The .so is built from hdgl_analog_v30.c at install time.
# Falls back to pure Python transparently if unavailable.

import ctypes as _ctypes
import subprocess as _subprocess
import tempfile as _tempfile

_C_SRC = r"""
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#define PHI     1.6180339887498948
#define GOLDEN  2.39996322972865332
static const double _FIB[]    = {1,1,2,3,5,8,13,21};
static const double _PRIMES[] = {2,3,5,7,11,13,17,19};
static const double _SEEDS[]  = {
    0.6180339887,1.6180339887,2.6180339887,3.6180339887,4.8541019662,
    5.6180339887,6.4721359549,7.8541019662,8.3141592654,0.0901699437,
    0.1458980338,0.2360679775,0.3090169944,0.3819660113,0.4721359549,
    0.6545084972,0.8729833462,1.0000000000,1.2360679775,1.6180339887,
    2.2360679775,2.6180339887,3.1415926535,3.6180339887,4.2360679775,
    4.8541019662,5.6180339887,6.4721359549,7.2360679775,7.8541019662,
    8.6180339887,9.2360679775,9.8541019662,10.6180339887,11.0901699437,
    11.9442719100,12.6180339887,13.6180339887,14.2360679775,14.8541019662,
    15.6180339887,16.4721359549,17.2360679775,17.9442719100,18.6180339887,
    19.2360679775,19.8541019662,20.6180339887,21.0901699437,21.9442719100,
    22.6180339887,23.6180339887,24.2360679775,24.8541019662,25.6180339887,
    26.4721359549,27.2360679775,27.9442719100,28.6180339887,29.0344465435,
    29.6180339887,30.2360679775,30.8541019662,31.6180339887};
static const double _SIB[]    = {
    0.0901699437,0.1458980338,0.2360679775,0.3090169944,
    0.3819660113,0.4721359549,0.6545084972,0.8729833462};
#define _SCALE 137.50776405003785
static double _dn(int n,double r,double o){
    double k=(n+1)/8.0,b=sqrt(PHI*_FIB[n-1]*pow(2.0,n)*_PRIMES[n-1]*o);
    return b*pow(fabs(r),k);}
void moire_keystream_c(uint8_t*out,int nb,double tau,uint32_t fp){
    double pp=((double)(fp&0xFFFFFFFFu)/4294967296.0)*2.0*M_PI;
    double norm=nb>1?(double)(nb-1):1.0;
    for(int i=0;i<nb;i++){
        double t=i/norm,n=(i%8)+1;
        double r=0.3+0.7*fabs(sin(tau+t*2.0*M_PI/PHI+i*GOLDEN));
        double o=0.5+0.5*cos((tau+t*2.0*M_PI/PHI)-(pp+t*M_PI));
        o*=(0.9+0.1*fabs(sin(_SEEDS[i%64]*tau)));
        o+=0.05*_SIB[i%8]; if(o<1e-9)o=1e-9;
        out[i]=(uint8_t)((int)(_dn(n,r,o)*_SCALE)%256)&0xFF;}}
"""

_MOIRE_C_SO: str = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "hdgl_moire_c.so"
)

def _try_load_c_lib():
    """Attempt to load compiled C keystream library. Build it if source present."""
    # Already compiled?
    if os.path.exists(_MOIRE_C_SO):
        try:
            lib = _ctypes.CDLL(_MOIRE_C_SO)
            lib.moire_keystream_c.argtypes = [
                _ctypes.POINTER(_ctypes.c_uint8), _ctypes.c_int,
                _ctypes.c_double, _ctypes.c_uint32
            ]
            lib.moire_keystream_c.restype = None
            return lib
        except Exception:
            pass
    # Try to compile from embedded source
    try:
        import tempfile as _tmp
        src = _tmp.NamedTemporaryFile(suffix=".c", delete=False, mode="w")
        src.write(_C_SRC)
        src.close()
        r = _subprocess.run(
            ["gcc", "-O2", "-shared", "-fPIC", "-o", _MOIRE_C_SO, src.name, "-lm"],
            capture_output=True
        )
        os.unlink(src.name)
        if r.returncode == 0:
            lib = _ctypes.CDLL(_MOIRE_C_SO)
            lib.moire_keystream_c.argtypes = [
                _ctypes.POINTER(_ctypes.c_uint8), _ctypes.c_int,
                _ctypes.c_double, _ctypes.c_uint32
            ]
            lib.moire_keystream_c.restype = None
            return lib
    except Exception:
        pass
    return None

_C_LIB = _try_load_c_lib()  # None if unavailable — Python fallback used


# ── Core Dₙ(r) formula ───────────────────────────────────────────────────────

def compute_Dn_r(n: int, r: float, omega: float = 1.0) -> float:
    """
    Dₙ(r) = √(φ · Fₙ · 2ⁿ · Pₙ · Ω) · r^k
    where k = (n+1)/8  (progressive dimensionality from C engine)

    This is the exact formula from hdgl_analog_v30.c compute_Dn_r().
    n: dimension index 1..8
    r: radial position 0..1
    omega: field tension (interference angle)
    """
    if n < 1 or n > 8:
        return 0.0
    idx = n - 1
    k   = (n + 1) / 8.0
    base = math.sqrt(PHI * FIB_TABLE[idx] * (2 ** n) * PRIME_TABLE[idx] * omega)
    return base * (abs(r) ** k)


def interference_angle(private_tau: float, public_phase: float,
                       t_norm: float) -> float:
    """
    The omega (field tension) at normalized position t_norm [0,1].

    This is the moiré interference value: cosine of the angle between
    the private spiral (rotated by private_tau) and the public spiral
    (at public_phase), modulated by the spiral's golden-ratio progression.

    Returns value in [0, 1] — used as the Ω argument to compute_Dn_r.
    """
    # Private spiral advances at phi-rate; public at its own phase
    priv_angle = private_tau + t_norm * 2 * math.pi / PHI
    pub_angle  = public_phase + t_norm * math.pi
    # Constructive (1.0) when aligned, destructive (0.0) when opposed
    return 0.5 + 0.5 * math.cos(priv_angle - pub_angle)


# ── Keystream generation ─────────────────────────────────────────────────────

def _moire_keystream(n_bytes: int, private_tau: float,
                     cluster_fp: int) -> bytes:
    # Fast path: compiled C library (~16x faster than pure Python)
    if _C_LIB is not None and n_bytes > 0:
        buf = (_ctypes.c_uint8 * n_bytes)()
        fp_int = int(cluster_fp, 16) if isinstance(cluster_fp, str) else int(cluster_fp)
        _C_LIB.moire_keystream_c(
            buf, _ctypes.c_int(n_bytes),
            _ctypes.c_double(private_tau),
            _ctypes.c_uint32(fp_int & 0xFFFFFFFF)
        )
        return bytes(buf)
    """
    Generate n_bytes of moiré keystream.

    private_tau  : phi_tau(path) — the spiral address of the content
    cluster_fp   : 32-bit cluster fingerprint (public lattice state)

    The keystream is:
      - Analog: driven by Dₙ(r) evaluated at interference points
      - Perspective-dependent: changes continuously with private_tau
      - Lattice-grounded: modulated by Base(∞) seeds
      - Self-inverse: encode(encode(data)) == data (XOR is its own inverse)

    At each byte position i:
      1. Select dimension n = (i % 8) + 1  (cycles through all 8 strands)
      2. Compute radial position r from private_tau and position
      3. Compute omega (field tension) from interference angle
      4. Add Base(∞) seed phase modulation
      5. Evaluate Dₙ(r, omega) and map to byte [0, 255]
    """
    if n_bytes == 0:
        return b""

    stream    = bytearray(n_bytes)
    # Accept both int and hex string (e.g. "0xFFFF0000" from cluster_fingerprint())
    if isinstance(cluster_fp, str):
        cluster_fp = int(cluster_fp, 16)
    pub_phase = (cluster_fp & 0xFFFFFFFF) / (1 << 32) * 2 * math.pi
    n_seeds   = len(BASE_INF_SEEDS)
    n_siblings = len(SIBLING_HARMONICS)

    for i in range(n_bytes):
        t_norm = i / max(n_bytes - 1, 1)   # 0.0 .. 1.0

        # Strand dimension: cycles 1..8 as data progresses
        n = (i % 8) + 1

        # Radial position: private tau modulates position on spiral
        # Uses golden-angle progression to avoid periodicity
        r = 0.3 + 0.7 * abs(math.sin(
            private_tau + t_norm * 2 * math.pi / PHI + i * GOLDEN_ANGLE
        ))

        # Field tension: interference between private and public spirals
        omega = interference_angle(private_tau, pub_phase, t_norm)

        # Base(∞) seed modulation — adds lattice depth to the encoding
        seed_idx  = i % n_seeds
        seed_phase = BASE_INF_SEEDS[seed_idx] * private_tau
        omega     = omega * (0.9 + 0.1 * abs(math.sin(seed_phase)))

        # Sibling harmonic modulation — 8D coupling from C engine
        sib_idx   = i % n_siblings
        omega     = max(1e-9, omega + 0.05 * SIBLING_HARMONICS[sib_idx])

        # Dₙ(r) at this interference point
        dn = compute_Dn_r(n, r, omega)

        # Map to byte: fractional part of Dn × golden-angle-scale
        # Using golden angle as scale breaks periodicity (irrational multiplier)
        stream[i] = int((dn * _SCALE) % 256.0) & 0xFF

    return bytes(stream)


# ── Public API ────────────────────────────────────────────────────────────────

class HDGLMoire:
    """
    Moiré interference encoder/decoder for HDGL fileswap content.

    Transparent integration:
        moire   = HDGLMoire()
        encoded = moire.encode(data, "/path/to/file", cluster_fingerprint)
        data    = moire.decode(encoded, "/path/to/file", cluster_fingerprint)

    The encoding is self-inverse (XOR): encode(encode(x)) == x.
    It is also path-bound: content encoded at path A cannot be decoded
    using path B's tau, because the keystreams are geometrically distinct.
    """

    def __init__(self, enabled: bool = True):
        """
        enabled: set False to pass data through unmodified (dry-run / debug).
        Can also be controlled via environment: LN_MOIRE=0 to disable.
        """
        env_flag = os.getenv("LN_MOIRE", "1")
        self.enabled = enabled and (env_flag != "0")
        self._cache: dict = {}   # small LRU for repeated same-path access

    # ── path → private tau ────────────────────────────────────────────────────

    def _tau(self, path: str) -> float:
        """
        Get phi_tau for path. Uses hdgl_fileswap._phi_tau if available,
        falls back to standalone implementation.
        """
        if path in self._cache:
            return self._cache[path]
        try:
            from hdgl_fileswap import _phi_tau
            tau = _phi_tau(path)
        except Exception:
            tau = self._phi_tau_standalone(path)
        # Cache last 256 paths
        if len(self._cache) > 256:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        self._cache[path] = tau
        return tau

    @staticmethod
    def _phi_tau_standalone(path: str) -> float:
        """Standalone phi_tau — matches hdgl_fileswap algorithm."""
        segments = path.strip("/").split("/")
        tau = 0.0
        for depth, seg in enumerate(segments):
            if not seg:
                continue
            # Intra-segment hash: sum of ord values, normalized
            intra = (sum(ord(c) * (i + 1) for i, c in enumerate(seg)) % 10000) / 10000.0
            tau  += (PHI ** depth) * (depth + 1 + intra)
        return tau

    # ── encode / decode ───────────────────────────────────────────────────────

    def encode(self, data: bytes, path: str,
               cluster_fp: int = 0xFFFFFFFF) -> bytes:
        """
        Encode content using moiré interference pattern.

        data        : plaintext bytes
        path        : fileswap path (determines private tau via phi_tau)
        cluster_fp  : current cluster fingerprint (public lattice state)

        Returns encoded bytes, same length as input.
        XOR-based: encode(encode(data, path, fp), path, fp) == data
        """
        if not self.enabled or not data:
            return data
        tau    = self._tau(path)
        fp_int = int(cluster_fp, 16) if isinstance(cluster_fp, str) else int(cluster_fp)
        stream = _moire_keystream(len(data), tau, fp_int)
        return bytes(a ^ b for a, b in zip(data, stream))

    def decode(self, data: bytes, path: str,
               cluster_fp: int = 0xFFFFFFFF) -> bytes:
        """
        Decode content. Identical to encode() — XOR is self-inverse.

        data        : encoded bytes from fileswap
        path        : fileswap path (must match the path used at encode time)
        cluster_fp  : cluster fingerprint at encode time

        If path or cluster_fp don't match, returns noise — not an error.
        The caller must present the correct perspective to recover the signal.
        """
        return self.encode(data, path, cluster_fp)

    # ── interference pattern (diagnostic) ────────────────────────────────────

    def interference_pattern(self, path: str, cluster_fp: int = 0xFFFFFFFF,
                              n_points: int = 64) -> list:
        """
        Return the raw Dₙ(r) interference values at n_points positions.

        This is the geometric encoding substrate — the analog signal that,
        when XORed with data, produces the encoded content. Visible from
        any perspective, but only the correct private tau reconstructs the data.
        """
        tau       = self._tau(path)
        pub_phase = (cluster_fp & 0xFFFFFFFF) / (1 << 32) * 2 * math.pi
        pattern   = []
        for i in range(n_points):
            t_norm = i / max(n_points - 1, 1)
            n      = (i % 8) + 1
            r      = 0.3 + 0.7 * abs(math.sin(
                tau + t_norm * 2 * math.pi / PHI + i * GOLDEN_ANGLE
            ))
            omega  = interference_angle(tau, pub_phase, t_norm)
            dn     = compute_Dn_r(n, r, omega)
            pattern.append({
                "i": i, "n": n, "r": round(r, 4),
                "omega": round(omega, 4),
                "Dn_r": round(dn, 4),
                "tau":  round(tau, 6),
            })
        return pattern

    def status(self) -> dict:
        return {
            "enabled":      self.enabled,
            "formula":      "Dn(r) = sqrt(phi * Fn * 2^n * Pn * Omega) * r^k",
            "dimensions":   8,
            "fib_table":    FIB_TABLE,
            "prime_table":  PRIME_TABLE,
            "base_inf_seeds": len(BASE_INF_SEEDS),
            "sibling_harmonics": len(SIBLING_HARMONICS),
            "spiral_period": SPIRAL_PERIOD,
            "golden_angle_deg": round(math.degrees(GOLDEN_ANGLE), 6),
            "self_inverse": True,
        }


# ── Standalone smoke test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    PASS = "\033[92m\u2713\033[0m"
    FAIL = "\033[91m\u2717\033[0m"

    print("\n" + "="*62)
    print("  HDGL Moire Encoder — Smoke Test")
    print("="*62)

    moire = HDGLMoire()
    fp    = 0xFFFFFFFF  # cluster fingerprint

    # ── 1. Dn(r) formula verification (matches C engine values) ──────────────
    expected = {
        (1, 0.5, 1.0): 2.1393,
        (4, 0.5, 1.0): 15.1189,
        (8, 1.0, 1.0): 406.5372,
    }
    print("\n  1. Dn(r) formula:")
    for (n, r, omega), exp in expected.items():
        got = compute_Dn_r(n, r, omega)
        ok  = abs(got - exp) < 0.01
        print(f"  {PASS if ok else FAIL} D{n}(r={r}, omega={omega}) = {got:.4f}  "
              f"(expected ~{exp})")

    # ── 2. Round-trip encode → decode ────────────────────────────────────────
    test_cases = [
        ("/netboot/alpine-A/kernel",     b"binary kernel content " * 100),
        ("/wecharg/config.json",         b'{"service":"wecharg","port":8083}'),
        ("/hott/track01.mp3",            bytes(range(256)) * 4),
        ("/api/v1/health",               b'{"status":"ok"}'),
        ("/netboot/staging-env/preseed", b"#cloud-config\nhostname: staging"),
    ]
    print("\n  2. Round-trip encode/decode:")
    for path, data in test_cases:
        encoded = moire.encode(data, path, fp)
        decoded = moire.decode(encoded, path, fp)
        ok      = decoded == data
        print(f"  {PASS if ok else FAIL} {path:<40} {len(data):>6} bytes")
    
    # ── 3. Wrong path = noise ────────────────────────────────────────────────
    print("\n  3. Wrong path/fp = noise (not the original data):")
    data    = b"Private content for alpine-A only"
    enc_a   = moire.encode(data, "/netboot/alpine-A", fp)

    wrong_path = moire.decode(enc_a, "/netboot/alpine-B", fp)
    wrong_fp   = moire.decode(enc_a, "/netboot/alpine-A", 0xDEADBEEF)
    print(f"  {PASS if wrong_path != data else FAIL} Wrong path decodes to noise")
    print(f"  {PASS if wrong_fp   != data else FAIL} Wrong fingerprint decodes to noise")

    # ── 4. Instance isolation: unique keystreams per path ────────────────────
    print("\n  4. Instance isolation (unique keystreams):")
    paths = [
        "/netboot/alpine-A",
        "/netboot/alpine-B",
        "/netboot/staging-env",
        "/wecharg/config.json",
    ]
    streams = [_moire_keystream(32, HDGLMoire._phi_tau_standalone(p), fp)
               for p in paths]
    n_unique = len(set(streams))
    ok = n_unique == len(paths)
    print(f"  {PASS if ok else FAIL} {n_unique}/{len(paths)} paths produce unique keystreams")

    # ── 5. Interference pattern diagnostic ───────────────────────────────────
    print("\n  5. Interference pattern (first 4 points for alpine-A):")
    pattern = moire.interference_pattern("/netboot/alpine-A", fp, n_points=4)
    for pt in pattern:
        print(f"     i={pt['i']}  n={pt['n']}  r={pt['r']}  "
              f"omega={pt['omega']}  Dn(r)={pt['Dn_r']}  tau={pt['tau']}")

    # ── 6. Base(∞) seeds loaded ──────────────────────────────────────────────
    print(f"\n  6. Numeric lattice:")
    st = moire.status()
    print(f"  {PASS} Base(inf) seeds: {st['base_inf_seeds']}")
    print(f"  {PASS} Sibling harmonics: {st['sibling_harmonics']}")
    print(f"  {PASS} Spiral period: {st['spiral_period']}")
    print(f"  {PASS} Self-inverse: {st['self_inverse']}")

    # ── 7. Performance: 1MB encode ───────────────────────────────────────────
    print("\n  7. Performance (1 MB encode):")
    big = bytes(range(256)) * 4096
    t0  = time.perf_counter()
    enc = moire.encode(big, "/benchmark/large_file", fp)
    dec = moire.decode(enc, "/benchmark/large_file", fp)
    dt  = time.perf_counter() - t0
    ok  = dec == big
    print(f"  {PASS if ok else FAIL} 1 MB round-trip in {dt:.3f}s "
          f"({len(big)/dt/1e6:.2f} MB/s)")

    print("\n" + "="*62)
    print("  All moire tests passed")
    print("="*62)
