# Frontierland v0.6-c-frontierland

Frontierland is a branch-specific MUD experience built on zchg protocol semantics.

It includes:

- A native terminal game loop in C.
- A browser-access gateway in Python.
- zchg protocol-gated session APIs.
- Cooperative host participation where active players contribute hosting while online.
- Deterministic room ownership and migration tracking when host topology changes.

## Quick Start

### 1) Run Terminal Mode

```bash
gcc -std=c11 -O2 -Wall -Wextra -o frontierland/mud_terminal frontierland/mud_terminal.c
./frontierland/mud_terminal
```

### 2) Run Browser Gateway

```bash
python3 frontierland/browser_gateway.py
```

Open:

```text
http://127.0.0.1:8091/
```

## What Makes Frontierland Different

### zchg Protocol Gating

Browser API calls require protocol headers:

- X-ZCHG-Scheme: zchg://
- X-ZCHG-Protocol: zchg://;v=0.6-frontierland

Session creation also enforces a zchg session URI.

### Cooperative Hosting While Playing

Each active player sends heartbeats with hosting status. The gateway:

- Tracks active host peers.
- Assigns room ownership deterministically across hosts.
- Records migration events when hosts join, leave, or stop hosting.

This provides a practical collaborative hosting model for live gameplay.

## Gameplay Commands

Terminal and browser command set:

- n / north
- s / south
- e / east
- w / west
- look
- map
- get / take
- inv / inventory
- talk
- help
- quit

## API Reference

### POST /api/session/start

Start or join a session URI.

Payload:

```json
{
	"user": "alice",
	"session_uri": "zchg://frontierland/session/main"
}
```

### POST /api/session/command

Execute player command within a session.

Payload:

```json
{
	"session_id": "...",
	"player_id": "...",
	"command": "look"
}
```

### POST /api/session/heartbeat

Update host participation and liveness.

Payload:

```json
{
	"session_id": "...",
	"player_id": "...",
	"hosting": true
}
```

### GET /api/session/state

Current player world state.

Query:

```text
/api/session/state?session_id=...&player_id=...
```

### GET /api/session/topology

Session host topology, room-owner map, and migration history.

Query:

```text
/api/session/topology?session_id=...
```

## Core Files

- frontierland/mud_terminal.c: terminal gameplay engine
- frontierland/browser_gateway.py: browser gateway, protocol gate, session and topology logic
- frontierland/README.md: branch-specific project guide

## Branch Contract

This README is scoped to v0.6-c-frontierland and documents Frontierland as a standalone experience track.
