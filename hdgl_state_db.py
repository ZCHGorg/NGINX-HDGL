#!/usr/bin/env python3
"""
hdgl_state_db.py
────────────────
SQLite-backed state persistence for the HDGL lattice.

Replaces pickle (unsafe for untrusted data, not forward-compatible).
Uses only Python stdlib sqlite3 — no external dependencies.

Schema
──────
lattice_ema    — per-node EMA latency values, timestamped
known_nodes    — discovered peer IPs with first_seen / last_seen
metadata       — key/value store for arbitrary host state

Migration
─────────
On first start, if a legacy lattice_state.pkl exists alongside this
module's DB path, _migrate_from_pickle() is called automatically.
The .pkl file is renamed to .pkl.migrated after a successful migration
so it is not processed again.

Usage in hdgl_host.py
─────────────────────
  from hdgl_state_db import HDGLStateDB

  db = HDGLStateDB(INSTALL_DIR / "lattice_state.db")
  db.open()

  # Load
  lat._latency_ema = db.load_ema()
  known_nodes      = db.load_known_nodes()

  # Save (call each cycle)
  db.save_ema(lat._latency_ema)
  db.save_known_nodes(known_nodes)

  # On shutdown
  db.close()
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("hdgl.state_db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lattice_ema (
    node        TEXT    PRIMARY KEY,
    latency_ema REAL    NOT NULL,
    last_seen   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS known_nodes (
    node        TEXT    PRIMARY KEY,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata (
    key         TEXT    PRIMARY KEY,
    value       TEXT    NOT NULL,
    updated_at  INTEGER NOT NULL
);
"""

# Prune nodes not seen in this many seconds (24 hours)
_STALE_CUTOFF = 86_400


class HDGLStateDB:
    """
    SQLite state store for HDGL lattice EMA, known nodes, and metadata.

    Thread-safe: uses check_same_thread=False with WAL journal mode.
    All writes are committed immediately (autocommit-style within transactions).
    """

    def __init__(self, path: Path):
        self.path  = Path(path)
        self._conn: Optional[sqlite3.Connection] = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def open(self) -> None:
        """Open (or create) the database. Idempotent."""
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,   # autocommit
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        log.debug(f"[state_db] opened: {self.path}")

    def close(self) -> None:
        """Flush WAL and close."""
        if self._conn:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
            except Exception as e:
                log.warning(f"[state_db] close error: {e}")
            finally:
                self._conn = None

    def _ensure_open(self) -> sqlite3.Connection:
        if self._conn is None:
            self.open()
        return self._conn  # type: ignore

    # ── EMA ───────────────────────────────────────────────────────────────────

    def save_ema(self, ema: Dict[str, float], timestamp: Optional[int] = None) -> None:
        """
        Persist per-node EMA latency values.
        Also prunes entries not updated in the last 24 hours.
        """
        ts   = timestamp if timestamp is not None else int(time.time())
        conn = self._ensure_open()
        with conn:
            for node, lat in ema.items():
                conn.execute(
                    "INSERT OR REPLACE INTO lattice_ema (node, latency_ema, last_seen)"
                    " VALUES (?, ?, ?)",
                    (node, float(lat), ts),
                )
            # Prune stale entries
            cutoff = ts - _STALE_CUTOFF
            conn.execute(
                "DELETE FROM lattice_ema WHERE last_seen < ?", (cutoff,)
            )

    def load_ema(self) -> Dict[str, float]:
        """
        Load all non-stale EMA values.
        Returns empty dict if table is empty or DB is new.
        """
        conn = self._ensure_open()
        cutoff = int(time.time()) - _STALE_CUTOFF
        rows   = conn.execute(
            "SELECT node, latency_ema FROM lattice_ema WHERE last_seen >= ?",
            (cutoff,),
        ).fetchall()
        result = {row[0]: row[1] for row in rows}
        if result:
            log.info(f"[state_db] loaded EMA for {len(result)} nodes")
        return result

    # ── Known nodes ───────────────────────────────────────────────────────────

    def save_known_nodes(self, nodes: List[str], timestamp: Optional[int] = None) -> None:
        """
        Persist peer node list.
        Updates last_seen on existing entries; inserts new ones.
        """
        ts   = timestamp if timestamp is not None else int(time.time())
        conn = self._ensure_open()
        with conn:
            for node in nodes:
                conn.execute(
                    "INSERT INTO known_nodes (node, first_seen, last_seen)"
                    " VALUES (?, ?, ?)"
                    " ON CONFLICT(node) DO UPDATE SET last_seen=excluded.last_seen",
                    (node, ts, ts),
                )
            # Prune nodes not seen in 24h
            cutoff = ts - _STALE_CUTOFF
            pruned = conn.execute(
                "SELECT COUNT(*) FROM known_nodes WHERE last_seen < ?", (cutoff,)
            ).fetchone()[0]
            if pruned:
                conn.execute(
                    "DELETE FROM known_nodes WHERE last_seen < ?", (cutoff,)
                )
                log.info(f"[state_db] pruned {pruned} stale known_nodes")

    def load_known_nodes(self) -> List[str]:
        """Load non-stale known peer IPs."""
        conn   = self._ensure_open()
        cutoff = int(time.time()) - _STALE_CUTOFF
        rows   = conn.execute(
            "SELECT node FROM known_nodes WHERE last_seen >= ? ORDER BY last_seen DESC",
            (cutoff,),
        ).fetchall()
        return [row[0] for row in rows]

    # ── Metadata ──────────────────────────────────────────────────────────────

    def save_metadata(self, key: str, value: str) -> None:
        conn = self._ensure_open()
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, int(time.time())),
        )

    def load_metadata(self, key: str) -> Optional[str]:
        conn = self._ensure_open()
        row  = conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    # ── Pickle migration ──────────────────────────────────────────────────────

    def migrate_from_pickle(self, pkl_path: Path) -> bool:
        """
        One-time migration: read legacy pickle, write into SQLite, rename .pkl.
        Returns True if migration was performed, False if skipped or failed.
        """
        if not pkl_path.exists():
            return False
        try:
            import pickle
            state   = pickle.loads(pkl_path.read_bytes())
            ema     = state.get("ema", {})
            ts      = int(state.get("timestamp", time.time()))
            if ema:
                self.save_ema(ema, timestamp=ts)
                log.info(
                    f"[state_db] migrated {len(ema)} EMA entries from "
                    f"{pkl_path.name}"
                )
            # Rename so we don't migrate again
            pkl_path.rename(pkl_path.with_suffix(".pkl.migrated"))
            return True
        except Exception as e:
            log.warning(f"[state_db] pickle migration failed: {e}")
            return False


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile, shutil

    td = Path(tempfile.mkdtemp(prefix="hdgl_state_test_"))
    db = HDGLStateDB(td / "test.db")
    db.open()

    # EMA round-trip
    ema_in = {"10.0.0.1": 45.2, "10.0.0.2": 62.1, "10.0.0.3": 30.0}
    db.save_ema(ema_in)
    ema_out = db.load_ema()
    assert set(ema_in.keys()) == set(ema_out.keys()), f"EMA keys mismatch: {ema_out}"
    for k in ema_in:
        assert abs(ema_in[k] - ema_out[k]) < 0.001, f"EMA value mismatch for {k}"
    print("✓ EMA round-trip")

    # Known nodes round-trip
    nodes_in = ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    db.save_known_nodes(nodes_in)
    nodes_out = db.load_known_nodes()
    assert set(nodes_in) == set(nodes_out), f"nodes mismatch: {nodes_out}"
    print("✓ Known nodes round-trip")

    # Metadata
    db.save_metadata("version", "2.0.1")
    assert db.load_metadata("version") == "2.0.1"
    assert db.load_metadata("missing") is None
    print("✓ Metadata")

    # Stale pruning
    old_ts = int(time.time()) - 90000   # 25h ago
    db.save_ema({"stale.node": 99.0}, timestamp=old_ts)
    db.save_ema({"10.0.0.1": 45.2})    # triggers prune
    fresh = db.load_ema()
    assert "stale.node" not in fresh, "stale node not pruned"
    print("✓ Stale EMA pruned")

    # Pickle migration
    import pickle
    pkl_path = td / "lattice_state.pkl"
    pkl_path.write_bytes(pickle.dumps(
        {"ema": {"10.0.0.9": 77.7}, "timestamp": time.time()}
    ))
    db2 = HDGLStateDB(td / "migrated.db")
    db2.open()
    ok = db2.migrate_from_pickle(pkl_path)
    assert ok, "migration returned False"
    assert not pkl_path.exists(), ".pkl not renamed"
    assert (td / "lattice_state.pkl.migrated").exists()
    ema_m = db2.load_ema()
    assert "10.0.0.9" in ema_m, f"migrated EMA missing: {ema_m}"
    print("✓ Pickle migration")

    db.close()
    db2.close()
    shutil.rmtree(td)

    print("\nAll hdgl_state_db tests passed.")
