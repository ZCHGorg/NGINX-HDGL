#!/usr/bin/env python3
"""Frontierland browser gateway with zchg:// protocol gating and cooperative hosting."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = 8091
MAX_INV = 8
MAP_W = 5
MAP_H = 5
HOST_TTL_SEC = 30


@dataclass
class RoomData:
    name: str
    description: str
    item: str | None = None
    npc: str | None = None
    npc_line: str | None = None


@dataclass
class PlayerState:
    player_id: str
    username: str
    x: int = 2
    y: int = 2
    inventory: List[str] = field(default_factory=list)
    hosting: bool = True
    last_seen: float = field(default_factory=time.time)


@dataclass
class SessionState:
    session_id: str
    session_uri: str
    created_at: float = field(default_factory=time.time)
    players: Dict[str, PlayerState] = field(default_factory=dict)
    taken_items: Dict[str, bool] = field(default_factory=dict)
    room_owner: Dict[str, str] = field(default_factory=dict)
    migrations: List[Dict[str, object]] = field(default_factory=list)
    chat_log: List[Dict[str, object]] = field(default_factory=list)


def room_name(x: int, y: int) -> str:
    names = {
        (2, 2): "Town Crossroads",
        (0, 0): "Northwest Ridge",
        (4, 0): "Northeast Watch",
        (0, 4): "Southwest Flats",
        (4, 4): "Southeast Gate",
    }
    return names.get((x, y), "Dust Trail")


def room_data(x: int, y: int) -> RoomData:
    base = RoomData(
        name=room_name(x, y),
        description="A wind-carved trail cuts through open scrubland.",
    )

    overrides: Dict[Tuple[int, int], RoomData] = {
        (2, 2): RoomData(
            name="Town Crossroads",
            description="Four roads meet beside a cracked stone well and old signpost.",
            item="rusty key",
            npc="marshal",
            npc_line="Keep your eyes open. Frontierland remembers every footprint.",
        ),
        (0, 0): RoomData(
            name="Northwest Ridge",
            description="A high ridge with wide views and cold wind from the canyon.",
            item="ridge map",
        ),
        (4, 0): RoomData(
            name="Northeast Watch",
            description="A weathered watch post overlooks the eastern perimeter.",
            item="signal flare",
            npc="lookout",
            npc_line="If smoke rises south, light the flare and run west.",
        ),
        (0, 4): RoomData(
            name="Southwest Flats",
            description="Dry grass waves over low flats where old tracks disappear.",
            item="canteen",
        ),
        (4, 4): RoomData(
            name="Southeast Gate",
            description="An iron gate marks the edge of settled ground.",
            item="gate token",
            npc="gatekeeper",
            npc_line="No one leaves empty-handed. Bring proof of purpose.",
        ),
    }

    return overrides.get((x, y), base)


def exits_for(x: int, y: int) -> List[str]:
    exits: List[str] = []
    if y > 0:
        exits.append("N")
    if y < MAP_H - 1:
        exits.append("S")
    if x < MAP_W - 1:
        exits.append("E")
    if x > 0:
        exits.append("W")
    return exits


def room_key(x: int, y: int) -> str:
    return f"{x},{y}"


def recalc_topology(session: SessionState) -> None:
    hosts = active_hosts(session)

    if not hosts:
        if session.room_owner:
            session.room_owner = {}
        return

    next_owner: Dict[str, str] = {}
    for y in range(MAP_H):
        for x in range(MAP_W):
            k = room_key(x, y)
            idx = (x + (y * MAP_W)) % len(hosts)
            next_owner[k] = hosts[idx].username

    now = int(time.time())
    for k, owner in next_owner.items():
        prev = session.room_owner.get(k)
        if prev is not None and prev != owner:
            session.migrations.append(
                {
                    "room": k,
                    "from": prev,
                    "to": owner,
                    "ts": now,
                }
            )

    session.room_owner = next_owner

    # Keep migration history bounded.
    if len(session.migrations) > 128:
        session.migrations = session.migrations[-128:]


def ensure_zchg_uri(session_uri: str) -> bool:
    return session_uri.strip().lower().startswith("zchg://")


def active_hosts(session: SessionState) -> List[PlayerState]:
    now = time.time()
    hosts = [
        p
        for p in session.players.values()
        if p.hosting and (now - p.last_seen) <= HOST_TTL_SEC
    ]
    hosts.sort(key=lambda p: p.username.lower())
    return hosts


def player_snapshot(session: SessionState, player: PlayerState) -> Dict[str, object]:
    room = room_data(player.x, player.y)
    item_visible = None if (room.item and session.taken_items.get(room_key(player.x, player.y), False)) else room.item
    recalc_topology(session)
    hosts = active_hosts(session)
    host_names = [p.username for p in hosts]

    assigned_host = session.room_owner.get(room_key(player.x, player.y))

    return {
        "x": player.x,
        "y": player.y,
        "name": room.name,
        "description": room.description,
        "item": item_visible,
        "npc": room.npc,
        "npc_line": room.npc_line,
        "exits": exits_for(player.x, player.y),
        "inventory": list(player.inventory),
        "session_uri": session.session_uri,
        "players_online": sorted([p.username for p in session.players.values()]),
        "active_hosts": host_names,
        "assigned_host": assigned_host,
        "recent_migrations": session.migrations[-5:],
        "recent_chat": session.chat_log[-10:],
    }


def topology_snapshot(session: SessionState) -> Dict[str, object]:
    recalc_topology(session)
    return {
        "session_uri": session.session_uri,
        "active_hosts": [p.username for p in active_hosts(session)],
        "room_owner": dict(session.room_owner),
        "migrations": session.migrations[-32:],
    }


def process_command(session: SessionState, player: PlayerState, cmd: str) -> Dict[str, object]:
    raw = cmd.strip()
    c = raw.lower()
    # Tolerate punctuation and spacing for directional commands.
    c_norm = " ".join(c.replace(",", " ").replace(";", " ").split())
    msg = ""

    if c.startswith("say "):
        text = raw[4:].strip()
        if not text:
            msg = "Say what?"
        else:
            entry = {
                "from": player.username,
                "text": text[:240],
                "ts": int(time.time()),
            }
            session.chat_log.append(entry)
            if len(session.chat_log) > 200:
                session.chat_log = session.chat_log[-200:]
            msg = f"You say: {entry['text']}"
        player.last_seen = time.time()
        return {"message": msg, "state": player_snapshot(session, player)}

    move_north = c_norm in ("n", "north", "up", "u")
    move_south = c_norm in ("s", "south", "down", "d")
    move_east = c_norm in ("e", "east", "right", "r")
    move_west = c_norm in ("w", "west", "left", "l")

    if move_north:
        if player.y == 0:
            msg = "A canyon wall blocks your way north."
        else:
            player.y -= 1
            msg = "You move north."
    elif move_south:
        if player.y == MAP_H - 1:
            msg = "The southern boundary is impassable scrub."
        else:
            player.y += 1
            msg = "You move south."
    elif move_east:
        if player.x == MAP_W - 1:
            msg = "You reach the eastern perimeter fence."
        else:
            player.x += 1
            msg = "You move east."
    elif move_west:
        if player.x == 0:
            msg = "A steep drop prevents heading further west."
        else:
            player.x -= 1
            msg = "You move west."
    elif c_norm == "look":
        msg = "You look around."
    elif c_norm in ("get", "take"):
        room = room_data(player.x, player.y)
        k = room_key(player.x, player.y)
        if not room.item:
            msg = "There is nothing to pick up here."
        elif session.taken_items.get(k, False):
            msg = "You already collected the item from this room."
        elif len(player.inventory) >= MAX_INV:
            msg = "Your inventory is full."
        else:
            player.inventory.append(room.item)
            session.taken_items[k] = True
            msg = f"You pick up: {room.item}"
    elif c_norm in ("inv", "inventory"):
        msg = "Inventory: " + (", ".join(player.inventory) if player.inventory else "empty")
    elif c_norm == "talk":
        room = room_data(player.x, player.y)
        if room.npc:
            msg = f"{room.npc} says: \"{room.npc_line}\""
        else:
            msg = "No one is here to talk to."
    elif c_norm == "map":
        msg = "Map requested."
    elif c_norm == "help":
        msg = "Commands: n s e w (or up/down/left/right), look, map, get, inv, talk, say <msg>, help"
    else:
        # QoL: treat freeform phrases as chat so users can type naturally.
        if " " in cmd.strip():
            entry = {
                "from": player.username,
                "text": cmd.strip()[:240],
                "ts": int(time.time()),
            }
            session.chat_log.append(entry)
            if len(session.chat_log) > 200:
                session.chat_log = session.chat_log[-200:]
            msg = f"You say: {entry['text']}"
        else:
            msg = f"Unknown command: {cmd}"

    player.last_seen = time.time()
    return {"message": msg, "state": player_snapshot(session, player)}


SESSIONS: Dict[str, SessionState] = {}


INDEX_HTML = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Frontierland zchg:// Terminal</title>
  <style>
    :root {
      --bg: #0f1419;
      --panel: #19232d;
      --ink: #e6edf3;
      --accent: #f4b860;
      --muted: #96a6b8;
      --ok: #63d471;
    }
    body {
      margin: 0;
      font-family: Consolas, \"Courier New\", monospace;
      background: radial-gradient(circle at 20% 20%, #1b2a38, #0f1419 55%);
      color: var(--ink);
    }
    .wrap { max-width: 1040px; margin: 24px auto; padding: 0 16px; }
    .panel {
      background: var(--panel);
      border: 1px solid #27323d;
      border-radius: 10px;
      padding: 14px;
      box-shadow: 0 10px 35px rgba(0,0,0,.35);
    }
    .cfg { display: grid; grid-template-columns: 1fr 1fr auto auto; gap: 8px; margin-bottom: 12px; }
    .terminal { min-height: 340px; max-height: 480px; overflow-y: auto; }
    .line { margin: 0 0 6px; white-space: pre-wrap; }
    .muted { color: var(--muted); }
    .accent { color: var(--accent); }
    .ok { color: var(--ok); }
    .cmdrow { display: flex; gap: 8px; margin-top: 12px; }
    .status { margin-top: 10px; font-size: 13px; color: var(--muted); }
    input, button {
      font: inherit;
      color: var(--ink);
      background: #111a23;
      border: 1px solid #2a3a4b;
      border-radius: 8px;
      padding: 10px;
    }
    input { width: 100%; }
    button { cursor: pointer; }
        .ghost {
            display: inline-block;
            text-decoration: none;
            text-align: center;
            color: var(--ink);
            background: #111a23;
            border: 1px solid #2a3a4b;
            border-radius: 8px;
            padding: 10px;
        }
  </style>
</head>
<body>
<div class=\"wrap\">
  <div class=\"panel\">
    <div class=\"cfg\">
      <input id=\"user\" placeholder=\"username\" value=\"rider\" />
      <input id=\"uri\" placeholder=\"zchg://frontierland/session/main\" value=\"zchg://frontierland/session/main\" />
      <button id=\"connect\">Connect</button>
            <a class="ghost" href="/unlock">Unlock zchg://</a>
    </div>
    <div class=\"terminal\" id=\"term\"></div>
    <div class=\"cmdrow\">
      <input id=\"cmd\" placeholder=\"Type command: n, s, e, w, look, get, talk...\" disabled />
      <button id=\"send\" disabled>Send</button>
    </div>
    <div class=\"status\" id=\"status\">Disconnected</div>
  </div>
</div>
<script>
const term = document.getElementById('term');
const cmd = document.getElementById('cmd');
const send = document.getElementById('send');
const connect = document.getElementById('connect');
const user = document.getElementById('user');
const uri = document.getElementById('uri');
const statusEl = document.getElementById('status');

let sessionId = null;
let playerId = null;
let hbTimer = null;

function add(text, klass = '') {
  const p = document.createElement('p');
  p.className = 'line ' + klass;
  p.textContent = text;
  term.appendChild(p);
  term.scrollTop = term.scrollHeight;
}

function setStatus(text) { statusEl.textContent = text; }

function renderState(state) {
  add(`You are at [${state.x},${state.y}] - ${state.name}`, 'accent');
  add(state.description, 'muted');
  if (state.item) add(`Item here: ${state.item}`);
  if (state.npc) add(`You see: ${state.npc}`);
  add(`Exits: ${state.exits.join(' ')}`);
  add(`Session: ${state.session_uri}`, 'muted');
  add(`Hosts online: ${state.active_hosts.join(', ') || 'none'}`, 'ok');
  if (state.assigned_host) add(`Assigned host for this room: ${state.assigned_host}`, 'ok');
    if (state.recent_chat && state.recent_chat.length) {
        add('--- Session Chat ---', 'muted');
        for (const m of state.recent_chat.slice(-3)) {
            add(`[${m.from}] ${m.text}`);
        }
    }
}

async function zfetch(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {}, {
    'X-ZCHG-Scheme': 'zchg://',
    'X-ZCHG-Protocol': 'zchg://;v=0.6-frontierland',
  });
  return fetch(path, Object.assign({}, opts, { headers }));
}

async function heartbeat() {
  if (!sessionId || !playerId) return;
  await zfetch('/api/session/heartbeat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, player_id: playerId, hosting: true }),
  });
}

async function doConnect() {
  const username = user.value.trim();
  const sessionUri = uri.value.trim();
  if (!username || !sessionUri) return;

  const r = await zfetch('/api/session/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user: username, session_uri: sessionUri }),
  });
  const data = await r.json();
  if (!r.ok) {
    add(`connect failed: ${data.error || 'unknown'}`);
    return;
  }

  sessionId = data.session_id;
  playerId = data.player_id;
  cmd.disabled = false;
  send.disabled = false;
  setStatus(`Connected as ${username} (${sessionUri})`);
  add('FRONTIERLAND browser terminal online', 'accent');
  add('Protocol gate: zchg:// verified', 'ok');
  add(data.message);
  renderState(data.state);

  if (hbTimer) clearInterval(hbTimer);
  hbTimer = setInterval(heartbeat, 10000);
  heartbeat();
}

async function run() {
  const value = cmd.value.trim();
  if (!value || !sessionId || !playerId) return;
  add(`frontierland> ${value}`);
  cmd.value = '';

  const r = await zfetch('/api/session/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, player_id: playerId, command: value }),
  });

  const data = await r.json();
  if (!r.ok) {
    add(`error: ${data.error || 'unknown'}`);
    return;
  }

  add(data.message);
  renderState(data.state);
}

connect.addEventListener('click', doConnect);
send.addEventListener('click', run);
cmd.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });

// Allow local zchg handler handoff: /?zchg_uri=zchg%3A%2F%2F...
const params = new URLSearchParams(window.location.search);
const inbound = params.get('zchg_uri');
if (inbound && inbound.toLowerCase().startsWith('zchg://')) {
    uri.value = inbound;
    add(`Inbound zchg URI received: ${inbound}`, 'ok');
}
</script>
</body>
</html>
"""


UNLOCK_HTML = """<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Unlock zchg:// Protocol</title>
    <style>
        :root {
            --bg: #101724;
            --panel: #182437;
            --ink: #e7eef7;
            --accent: #f2b26a;
            --muted: #9cb1c9;
            --ok: #67d67d;
            --warn: #ffd166;
        }
        body {
            margin: 0;
            min-height: 100vh;
            font-family: Consolas, \"Courier New\", monospace;
            color: var(--ink);
            background:
                radial-gradient(circle at 10% 10%, rgba(80,130,190,.25), transparent 45%),
                radial-gradient(circle at 90% 85%, rgba(242,178,106,.2), transparent 40%),
                var(--bg);
            display: grid;
            place-items: center;
            padding: 20px;
        }
        .panel {
            width: min(760px, 95vw);
            background: var(--panel);
            border: 1px solid #2d415b;
            border-radius: 12px;
            padding: 18px;
            box-shadow: 0 14px 32px rgba(0,0,0,.35);
        }
        h1 { margin: 0 0 10px; color: var(--accent); font-size: 24px; }
        p { margin: 8px 0; color: var(--muted); line-height: 1.4; }
        .strong { color: var(--ink); }
        .row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
        button, a {
            font: inherit;
            color: var(--ink);
            background: #111c2b;
            border: 1px solid #355073;
            border-radius: 8px;
            padding: 10px 12px;
            text-decoration: none;
            cursor: pointer;
        }
        .status { margin-top: 12px; padding: 10px; border-radius: 8px; border: 1px solid #2d415b; }
        .ok { color: var(--ok); }
        .warn { color: var(--warn); }
        code {
            color: var(--ink);
            background: #122032;
            border: 1px solid #2a3f5a;
            border-radius: 6px;
            padding: 2px 5px;
        }
        ul { margin: 10px 0 0 18px; color: var(--muted); }
    </style>
</head>
<body>
    <div class=\"panel\">
        <h1>zchg:// Protocol Unlock</h1>
        <p class=\"strong\">This page can trigger zchg:// from a trusted user click.</p>
        <p>Browsers do not allow websites to silently install protocol handlers for arbitrary schemes. A local zchg handler must already be installed on this machine.</p>
        <p>Target URI: <code id=\"target\">zchg://frontierland/session/main</code></p>

        <div class=\"row\">
            <button id=\"unlock\">Unlock zchg:// now</button>
            <a href=\"/\">Open Frontierland Terminal</a>
        </div>

        <div class=\"status\" id=\"status\">Waiting for user action.</div>

        <ul>
            <li>If your browser prompts to open zchg://, allow it and optionally set always allow.</li>
            <li>If nothing happens, install/register your zchg protocol handler first, then retry.</li>
            <li>After successful launch, return to the terminal page and connect.</li>
        </ul>
    </div>

    <script>
        const unlockBtn = document.getElementById('unlock');
        const statusEl = document.getElementById('status');
        const target = 'zchg://frontierland/session/main';

        function setStatus(text, cls = '') {
            statusEl.className = 'status ' + cls;
            statusEl.textContent = text;
        }

        unlockBtn.addEventListener('click', () => {
            setStatus('Attempting to open zchg:// via user gesture...', 'warn');

            // Custom protocol launches are browser/OS mediated and require user action.
            window.location.href = target;

            setTimeout(() => {
                setStatus(
                    'If a prompt appeared, approve opening zchg://. If no prompt appeared, install or register a local zchg protocol handler and try again.',
                    'ok'
                );
            }, 900);
        });
    </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: Dict[str, object]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-ZCHG-Scheme", "zchg://")
        self.send_header("X-ZCHG-Protocol", "zchg://;v=0.6-frontierland")
        self.end_headers()
        self.wfile.write(data)

    def _protocol_ok(self) -> bool:
        return self.headers.get("X-ZCHG-Scheme", "") == "zchg://"

    def _read_json(self) -> Dict[str, object] | None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None

    def _session_and_player(self, payload: Dict[str, object]) -> Tuple[SessionState | None, PlayerState | None]:
        session_id = str(payload.get("session_id", "")).strip()
        player_id = str(payload.get("player_id", "")).strip()
        session = SESSIONS.get(session_id)
        if not session:
            return None, None
        player = session.players.get(player_id)
        if not player:
            return session, None
        return session, player

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/unlock":
            html = UNLOCK_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(html)
            return

        if parsed.path == "/":
            html = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(html)
            return

        if parsed.path == "/api/session/state":
            if not self._protocol_ok():
                self._send_json(426, {"error": "zchg:// protocol header required"})
                return

            q = parse_qs(parsed.query)
            session_id = q.get("session_id", [""])[0]
            player_id = q.get("player_id", [""])[0]
            session = SESSIONS.get(session_id)
            if not session:
                self._send_json(404, {"error": "session not found"})
                return
            player = session.players.get(player_id)
            if not player:
                self._send_json(404, {"error": "player not found"})
                return
            player.last_seen = time.time()
            self._send_json(200, {"state": player_snapshot(session, player)})
            return

        if parsed.path == "/api/session/topology":
            if not self._protocol_ok():
                self._send_json(426, {"error": "zchg:// protocol header required"})
                return

            q = parse_qs(parsed.query)
            session_id = q.get("session_id", [""])[0]
            session = SESSIONS.get(session_id)
            if not session:
                self._send_json(404, {"error": "session not found"})
                return

            self._send_json(200, {"topology": topology_snapshot(session)})
            return

        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if not self._protocol_ok():
            self._send_json(426, {"error": "zchg:// protocol header required"})
            return

        payload = self._read_json()
        if payload is None:
            self._send_json(400, {"error": "invalid json"})
            return

        if parsed.path == "/api/session/start":
            username = str(payload.get("user", "")).strip()
            session_uri = str(payload.get("session_uri", "")).strip()

            if not username:
                self._send_json(400, {"error": "user is required"})
                return
            if not ensure_zchg_uri(session_uri):
                self._send_json(400, {"error": "session_uri must start with zchg://"})
                return

            session = None
            for s in SESSIONS.values():
                if s.session_uri == session_uri:
                    session = s
                    break

            if session is None:
                session = SessionState(session_id=uuid.uuid4().hex[:12], session_uri=session_uri)
                SESSIONS[session.session_id] = session

            player_id = uuid.uuid4().hex[:10]
            player = PlayerState(player_id=player_id, username=username)
            session.players[player_id] = player
            recalc_topology(session)

            self._send_json(
                200,
                {
                    "session_id": session.session_id,
                    "player_id": player_id,
                    "message": f"Connected to {session_uri} as {username}",
                    "state": player_snapshot(session, player),
                },
            )
            return

        if parsed.path == "/api/session/command":
            session, player = self._session_and_player(payload)
            if not session:
                self._send_json(404, {"error": "session not found"})
                return
            if not player:
                self._send_json(404, {"error": "player not found"})
                return

            command = str(payload.get("command", "")).strip()
            if not command:
                self._send_json(400, {"error": "command required"})
                return

            result = process_command(session, player, command)
            self._send_json(200, result)
            return

        if parsed.path == "/api/session/heartbeat":
            session, player = self._session_and_player(payload)
            if not session:
                self._send_json(404, {"error": "session not found"})
                return
            if not player:
                self._send_json(404, {"error": "player not found"})
                return

            player.hosting = bool(payload.get("hosting", True))
            player.last_seen = time.time()
            recalc_topology(session)
            self._send_json(
                200,
                {
                    "ok": True,
                    "active_hosts": [p.username for p in active_hosts(session)],
                    "owned_rooms": [k for k, owner in session.room_owner.items() if owner == player.username],
                },
            )
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    server = HTTPServer((HOST, PORT), Handler)
    print(f"Frontierland browser gateway listening on http://{HOST}:{PORT}")
    print("Protocol gate enabled: send X-ZCHG-Scheme: zchg://")
    server.serve_forever()


if __name__ == "__main__":
    main()