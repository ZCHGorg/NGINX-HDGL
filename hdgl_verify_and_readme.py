#!/usr/bin/env python3
"""
hdgl_verify_and_readme.py
-------------------------
Lightweight repository verifier and DEPLOY_README generator.

What it does:
1. Syntax-checks all tracked Python modules in this repository.
2. Runs hdgl_audit.py and records pass/fail summary (if parsable).
3. Writes DEPLOY_README.md with current pointers to canonical docs.

This script intentionally avoids hard-coded historical claims (for example,
fixed test counts or legacy architecture references) so generated output stays
consistent with the repository as it evolves.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Tuple

ROOT = Path(__file__).parent

PY_FILES = [
    "hdgl_audit.py",
    "hdgl_dns.py",
    "hdgl_fileswap.py",
    "hdgl_host.py",
    "hdgl_ingress.py",
    "hdgl_lattice.py",
    "hdgl_moire.py",
    "hdgl_netboot.py",
    "hdgl_node_server.py",
    "hdgl_site_config.py",
    "hdgl_stability_sim.py",
    "hdgl_state_db.py",
    "hdgl_verify_and_readme.py",
]

DOC_FILES = [
    "README.md",
    "COMMAND_VERIFICATION_MATRIX.md",
    "RESET_AND_DEPLOY.md",
    "deploy_hdgl.sh",
]


def syntax_check() -> Tuple[bool, Dict[str, str]]:
    results: Dict[str, str] = {}
    ok = True
    for name in PY_FILES:
        path = ROOT / name
        if not path.exists():
            ok = False
            results[name] = "MISSING"
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"))
            results[name] = "PASS"
        except SyntaxError as exc:
            ok = False
            results[name] = f"FAIL line {exc.lineno}: {exc.msg}"
    return ok, results


def run_audit() -> Tuple[bool, str]:
    audit_path = ROOT / "hdgl_audit.py"
    if not audit_path.exists():
        return False, "hdgl_audit.py missing"

    try:
        env = dict(os.environ)
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(audit_path)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=180,
        )
    except Exception as exc:
        return False, f"audit execution failed: {exc}"

    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    # Remove ANSI escape codes and other control sequences that can split tokens.
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)

    # Supports:
    # - "57/57 passed"
    # - "57/57 tests passing"
    # - "HDGL FULL AUDIT: 57/57 passed"
    m = re.search(
        r"(?:FULL\s+AUDIT:\s*)?(\d+)\s*/\s*(\d+)\s*(?:passed|tests?\s+passing)",
        text,
        re.I,
    )
    if m:
        passed, total = int(m.group(1)), int(m.group(2))
        return passed == total, f"{passed}/{total}"

    if proc.returncode == 0:
        return True, "PASS (no numeric summary parsed)"

    return False, "FAIL (no numeric summary parsed)"


def write_deploy_readme(syntax_ok: bool, syntax_results: Dict[str, str], audit_ok: bool, audit_result: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    syntax_rows = "\n".join(
        f"| `{name}` | {syntax_results.get(name, 'MISSING')} |" for name in PY_FILES
    )

    docs_rows = "\n".join(
        f"- `{name}` {'OK' if (ROOT / name).exists() else 'MISSING'}" for name in DOC_FILES
    )

    content = f"""# HDGL Deploy README

Generated: {now}

## Verification Summary

- Syntax check: {'PASS' if syntax_ok else 'FAIL'}
- Audit check: {'PASS' if audit_ok else 'FAIL'} ({audit_result})

### Python Syntax Results

| File | Status |
|---|---|
{syntax_rows}

### Canonical Docs Present

{docs_rows}

## Operator Docs

Use these as source of truth:

1. `README.md`
2. `COMMAND_VERIFICATION_MATRIX.md`
3. `RESET_AND_DEPLOY.md`

## Deploy Model

- `deploy_hdgl.sh` handles package install, virtualenv setup, deploy user creation,
    systemd wiring, firewall defaults, and site config generation.
- `/opt/hdgl/.env` stores runtime flags and the cluster HMAC secret.
- `/opt/hdgl/site_config.json` stores domains, peers, storage paths, and services.
- The deploy script can start in simulation or live mode and auto-generates a
    cluster secret if one is not supplied.

## Notes

- This generated document is intentionally concise and references canonical docs.
- It avoids embedding stale operational claims.
"""

    (ROOT / "DEPLOY_README.md").write_text(content, encoding="utf-8")


def main() -> int:
    syntax_ok, syntax_results = syntax_check()
    audit_ok, audit_result = run_audit()

    write_deploy_readme(syntax_ok, syntax_results, audit_ok, audit_result)

    print("Syntax:", "PASS" if syntax_ok else "FAIL")
    print("Audit:", "PASS" if audit_ok else "FAIL", f"({audit_result})")
    print("Wrote DEPLOY_README.md")

    return 0 if (syntax_ok and audit_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
