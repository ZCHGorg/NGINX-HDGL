# HDGL README Command Verification Matrix

Scope: command-by-command verification of executable snippets in README against implementation and deploy sources.

Status legend:
- Exact: README command matches current code/deploy behavior directly.
- Environment-specific: command is valid but depends on local topology, files, service state, or distro policy.
- Advisory: command is operational guidance or migration surgery; usable with caution and local validation.

## Matrix

| README snippet | Status | Source of truth | Verification notes |
|---|---|---|---|
| [SCP upload bundle](README.md#L129) | Exact | [deploy required files](deploy_hdgl.sh#L191), [copy loop](deploy_hdgl.sh#L213) | README file list matches deploy required Python modules and script; optional `.so` is handled conditionally in deploy script. |
| [Deploy invocation with HDGL_LOCAL_NODE/HDGL_PEER_NODES](README.md#L145) | Exact | [env vars consumed by deploy](deploy_hdgl.sh#L45), [host mode example](hdgl_host.py#L35) | Command shape is correct for initial bootstrap on a node. |
| [Generate cluster secret + apply in .env](README.md#L155) | Exact | [cluster secret used by node auth](hdgl_node_server.py#L55), [HMAC verification](hdgl_node_server.py#L73) | Shared secret is required for authenticated gossip/invalidation in production mode. |
| [Set LN_SIMULATION and LN_DRY_RUN to 0](README.md#L159) | Exact | [host simulation flag](hdgl_host.py#L133), [host dry-run flag](hdgl_host.py#L134), [go-live guidance](hdgl_host.py#L564) | Correct go-live transition for daemon behavior. |
| [Verify cluster via /node_info](README.md#L170) | Exact | [/node_info handler](hdgl_node_server.py#L220), [health loop fetches /node_info](hdgl_host.py#L575) | Endpoint and payload use match runtime health checks. |
| [Cycle log tail filter](README.md#L173) | Exact | [cycle summary emits peers/fp_match/my_strands](hdgl_host.py#L490), [summary log line](hdgl_host.py#L499) | Grep terms map to actual log fields. |
| [Ghost peer DB cleanup (python sqlite block)](README.md#L261) | Advisory | [state DB path](hdgl_host.py#L139), [known node sanitization path](hdgl_host.py#L602) | Valid emergency maintenance; ensure service restart and verify no legitimate nodes are deleted. |
| [EMA reset for strand rebalancing](README.md#L277) | Advisory | [EMA used in weight updates](hdgl_host.py#L298), [per-cycle lattice updates](hdgl_host.py#L301) | Valid for forced recalibration; causes temporary routing instability while EMA re-learns. |
| [UFW ordering check / delete blanket deny](README.md#L292) | Environment-specific | [deploy adds deny + peer allows](deploy_hdgl.sh#L474), [peer allow rules](deploy_hdgl.sh#L482) | Depends on existing firewall state and rule ordering; command is valid for deploy profile. |
| [Fix nginx conf ownership](README.md#L302) | Environment-specific | [daemon writes living_network.conf](deploy_hdgl.sh#L375), [nginx conf path](hdgl_ingress.py#L40) | Needed only where permissions drift; not always required on every install. |
| [Service control commands](README.md#L322) | Exact | [service unit creation](deploy_hdgl.sh#L410), [sudoers allow start/stop/restart](deploy_hdgl.sh#L145) | Start/stop/restart operations are correct for managed service lifecycle. |
| [Journal follow command](README.md#L327) | Exact | [service name](deploy_hdgl.sh#L51), [deploy diagnostic guidance](deploy_hdgl.sh#L537) | Correct for runtime daemon diagnostics. |
| [Live API probes /health /metrics /strand_map /serve](README.md#L362) | Exact | [GET routing dispatch](hdgl_node_server.py#L218), [metrics handler](hdgl_node_server.py#L509), [strand map handler](hdgl_node_server.py#L533), [serve handler](hdgl_node_server.py#L304) | Endpoints and semantics align with handler implementation. |
| [State DB surgery via heredoc python](README.md#L389) | Advisory | [state DB location](hdgl_host.py#L139), [state usage](hdgl_host.py#L181) | Safe if executed exactly; always stop/restart service around invasive writes in production windows. |
| [Full reset sequence with pkill/routes cleanup](README.md#L421) | Advisory | [daemon unit start path](deploy_hdgl.sh#L410), [routes file path](hdgl_fileswap.py#L482), [swap root default](hdgl_fileswap.py#L154) | Valid emergency reset; `routes.bin` may not exist on current builds, but removal is harmless. |
| [Install updated python modules into /opt/hdgl](README.md#L438) | Exact | [deploy install directory](deploy_hdgl.sh#L46), [service executes from /opt/hdgl](deploy_hdgl.sh#L421) | Correct hot-update file replacement pattern followed by daemon restart. |
| [NGINX test command](README.md#L450) | Exact | [deploy nginx test path](deploy_hdgl.sh#L387), [ingress reload behavior](hdgl_ingress.py#L542) | Correct pre/post-change validation command. |
| [Restart daemon to regenerate nginx](README.md#L453) | Exact | [health loop calls nginx update each cycle](hdgl_host.py#L260), [generate_nginx_conf call](hdgl_host.py#L431) | Restart triggers immediate regeneration path on startup/cycle. |
| [Inspect upstream blocks in living_network.conf / hdgl_upstreams.conf](README.md#L456) | Environment-specific | [conf paths in deploy](deploy_hdgl.sh#L52), [placeholder upstream file](deploy_hdgl.sh#L379), [ingress default conf path](hdgl_ingress.py#L40) | Correct for deploy profile; fallback file is placeholder in current script. |
| [Socket/process inspection commands](README.md#L467) | Exact | [node server bind](hdgl_node_server.py#L619), [port default](hdgl_node_server.py#L169) | `ss` and process checks align with runtime listener model on :8090. |

## Cross-check deltas found and resolved

- README now reflects alpha-aware TTL model used by fileswap.
- README no longer references non-repo discourse sync script.
- README uses robust env substitutions for go-live toggles.
- README logrotate block reflects deploy-installed policy.

## Remaining intentional environment-specific areas

- Firewall commands vary by host policy and existing UFW chain state.
- Ownership fixes vary by how service user and nginx were provisioned.
- Hardcoded IP examples are illustrative and must be replaced per node.
- DB surgery snippets are intentionally operational, not regular-flow commands.
