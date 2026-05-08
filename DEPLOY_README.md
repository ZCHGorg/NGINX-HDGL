# HDGL Deploy README

Generated: 2026-05-08 19:19 UTC

## Verification Summary

- Syntax check: PASS
- Audit check: PASS (57/57)

### Python Syntax Results

| File | Status |
|---|---|
| `hdgl_audit.py` | PASS |
| `hdgl_dns.py` | PASS |
| `hdgl_fileswap.py` | PASS |
| `hdgl_host.py` | PASS |
| `hdgl_ingress.py` | PASS |
| `hdgl_lattice.py` | PASS |
| `hdgl_moire.py` | PASS |
| `hdgl_netboot.py` | PASS |
| `hdgl_node_server.py` | PASS |
| `hdgl_site_config.py` | PASS |
| `hdgl_stability_sim.py` | PASS |
| `hdgl_state_db.py` | PASS |
| `hdgl_verify_and_readme.py` | PASS |

### Canonical Docs Present

- `README.md` OK
- `COMMAND_VERIFICATION_MATRIX.md` OK
- `RESET_AND_DEPLOY.md` OK
- `deploy_hdgl.sh` OK

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
