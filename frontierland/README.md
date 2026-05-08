# Frontierland Terminal MUD (Prototype)

A minimal terminal-based movement loop with cardinal navigation.

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
