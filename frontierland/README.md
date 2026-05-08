# Frontierland Terminal MUD (Prototype)

A minimal terminal-based movement loop with cardinal navigation.

## Browser Terminal (zchg:// Gateway)

Run the browser gateway:

```bash
python3 frontierland/browser_gateway.py
```

Then open:

```text
http://127.0.0.1:8091/
```

### Protocol Gate

The browser bridge enforces zchg intent via headers:

- `X-ZCHG-Scheme: zchg://`
- `X-ZCHG-Protocol: zchg://;v=0.6-frontierland`

Session creation also requires a URI that starts with `zchg://`.

### Cooperative Hosting While Playing

Each connected player sends periodic heartbeat updates with `hosting=true`.
The gateway tracks active host peers and computes an assigned host per room.
This creates a practical frontier model where all active players contribute host capacity.

Room ownership is deterministic across active hosts and rebalanced as hosts
join/leave (migration events are tracked in topology history).

## Build

```bash
gcc -std=c11 -O2 -Wall -Wextra -o frontierland/mud_terminal frontierland/mud_terminal.c
```

## Run

```bash
./frontierland/mud_terminal
```

## Commands

- `n` / `north`
- `s` / `south`
- `e` / `east`
- `w` / `west`
- `look`
- `map`
- `get` / `take` (pick up room item)
- `inv` / `inventory` (show inventory)
- `talk` (speak with room NPC if present)
- `help`
- `quit`

## Browser API (for integration)

- `POST /api/session/start` with `{ "user": "name", "session_uri": "zchg://..." }`
- `POST /api/session/command` with `{ "session_id": "...", "player_id": "...", "command": "look" }`
- `POST /api/session/heartbeat` with `{ "session_id": "...", "player_id": "...", "hosting": true }`
- `GET /api/session/state?session_id=...&player_id=...`
- `GET /api/session/topology?session_id=...` (room owners + migration history)
