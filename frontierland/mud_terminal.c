#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAP_W 5
#define MAP_H 5
#define INPUT_LEN 128
#define INV_CAP 8

typedef struct {
    const char *name;
    const char *description;
    const char *item;
    const char *npc;
    const char *npc_line;
} room_t;

typedef struct {
    int x;
    int y;
} player_t;

typedef struct {
    player_t player;
    room_t rooms[MAP_H][MAP_W];
    int item_taken[MAP_H][MAP_W];
    const char *inventory[INV_CAP];
    int inv_count;
} game_t;

static void init_room(room_t *room,
                      const char *name,
                      const char *description,
                      const char *item,
                      const char *npc,
                      const char *npc_line) {
    room->name = name;
    room->description = description;
    room->item = item;
    room->npc = npc;
    room->npc_line = npc_line;
}

static void init_game(game_t *game) {
    int y;
    int x;

    memset(game, 0, sizeof(*game));
    game->player.x = 2;
    game->player.y = 2;

    for (y = 0; y < MAP_H; y++) {
        for (x = 0; x < MAP_W; x++) {
            init_room(&game->rooms[y][x],
                      "Dust Trail",
                      "A wind-carved trail cuts through open scrubland.",
                      NULL,
                      NULL,
                      NULL);
        }
    }

    init_room(&game->rooms[2][2],
              "Town Crossroads",
              "Four roads meet beside a cracked stone well and old signpost.",
              "rusty key",
              "marshal",
              "Keep your eyes open. Frontierland remembers every footprint.");

    init_room(&game->rooms[0][0],
              "Northwest Ridge",
              "A high ridge with wide views and cold wind from the canyon.",
              "ridge map",
              NULL,
              NULL);

    init_room(&game->rooms[0][4],
              "Northeast Watch",
              "A weathered watch post overlooks the eastern perimeter.",
              "signal flare",
              "lookout",
              "If smoke rises south, light the flare and run west.");

    init_room(&game->rooms[4][0],
              "Southwest Flats",
              "Dry grass waves over low flats where old tracks disappear.",
              "canteen",
              NULL,
              NULL);

    init_room(&game->rooms[4][4],
              "Southeast Gate",
              "An iron gate marks the edge of settled ground.",
              "gate token",
              "gatekeeper",
              "No one leaves empty-handed. Bring proof of purpose.");
}

static room_t *current_room(game_t *game) {
    return &game->rooms[game->player.y][game->player.x];
}

static void trim_newline(char *s) {
    size_t n = strlen(s);
    if (n > 0 && s[n - 1] == '\n') {
        s[n - 1] = '\0';
    }
}

static void lowercase(char *s) {
    while (*s) {
        *s = (char)tolower((unsigned char)*s);
        s++;
    }
}

static void print_intro(void) {
    printf("========================================\n");
    printf("  FRONTIERLAND TERMINAL ONLINE\n");
    printf("  MUD Link: ACTIVE\n");
    printf("========================================\n\n");
    printf("Commands: n s e w, look, map, get, inv, talk, help, quit\n\n");
}

static void print_location(game_t *game) {
    player_t *p = &game->player;
    room_t *room = current_room(game);

    printf("You are at [%d,%d] - %s\n", p->x, p->y, room->name);
    printf("%s\n", room->description);

    if (room->item && !game->item_taken[p->y][p->x]) {
        printf("Item here: %s\n", room->item);
    }
    if (room->npc) {
        printf("You see: %s\n", room->npc);
    }

    printf("Exits: ");
    if (p->y > 0) {
        printf("N ");
    }
    if (p->y < MAP_H - 1) {
        printf("S ");
    }
    if (p->x < MAP_W - 1) {
        printf("E ");
    }
    if (p->x > 0) {
        printf("W ");
    }
    printf("\n");
}

static void print_map(const game_t *game) {
    const player_t *p = &game->player;
    int y;
    int x;
    printf("\nMap (%dx%d):\n", MAP_W, MAP_H);
    for (y = 0; y < MAP_H; y++) {
        for (x = 0; x < MAP_W; x++) {
            if (x == p->x && y == p->y) {
                printf("[P]");
            } else {
                printf("[ ]");
            }
        }
        printf("\n");
    }
    printf("\n");
}

static void print_inventory(const game_t *game) {
    int i;
    if (game->inv_count == 0) {
        printf("Inventory is empty.\n");
        return;
    }

    printf("Inventory:\n");
    for (i = 0; i < game->inv_count; i++) {
        printf("- %s\n", game->inventory[i]);
    }
}

static void pickup_item(game_t *game) {
    player_t *p = &game->player;
    room_t *room = current_room(game);

    if (!room->item) {
        printf("There is nothing to pick up here.\n");
        return;
    }

    if (game->item_taken[p->y][p->x]) {
        printf("You already collected the item from this room.\n");
        return;
    }

    if (game->inv_count >= INV_CAP) {
        printf("Your inventory is full.\n");
        return;
    }

    game->inventory[game->inv_count++] = room->item;
    game->item_taken[p->y][p->x] = 1;
    printf("You pick up: %s\n", room->item);
}

static void talk_npc(game_t *game) {
    room_t *room = current_room(game);
    if (!room->npc) {
        printf("No one is here to talk to.\n");
        return;
    }

    printf("%s says: \"%s\"\n", room->npc, room->npc_line ? room->npc_line : "...");
}

static int move_player(player_t *p, const char *cmd) {
    if (strcmp(cmd, "n") == 0 || strcmp(cmd, "north") == 0) {
        if (p->y == 0) {
            printf("A canyon wall blocks your way north.\n");
            return 0;
        }
        p->y--;
        return 1;
    }

    if (strcmp(cmd, "s") == 0 || strcmp(cmd, "south") == 0) {
        if (p->y == MAP_H - 1) {
            printf("The southern boundary is impassable scrub.\n");
            return 0;
        }
        p->y++;
        return 1;
    }

    if (strcmp(cmd, "e") == 0 || strcmp(cmd, "east") == 0) {
        if (p->x == MAP_W - 1) {
            printf("You reach the eastern perimeter fence.\n");
            return 0;
        }
        p->x++;
        return 1;
    }

    if (strcmp(cmd, "w") == 0 || strcmp(cmd, "west") == 0) {
        if (p->x == 0) {
            printf("A steep drop prevents heading further west.\n");
            return 0;
        }
        p->x--;
        return 1;
    }

    return 0;
}

int main(void) {
    char input[INPUT_LEN];
    game_t game;

    init_game(&game);

    print_intro();
    print_location(&game);

    for (;;) {
        printf("\nfrontierland> ");
        if (!fgets(input, sizeof(input), stdin)) {
            printf("\nConnection closed.\n");
            break;
        }

        trim_newline(input);
        lowercase(input);

        if (input[0] == '\0') {
            continue;
        }

        if (strcmp(input, "quit") == 0 || strcmp(input, "exit") == 0) {
            printf("Session ended. See you in Frontierland.\n");
            break;
        }

        if (strcmp(input, "help") == 0) {
            printf("Commands: n s e w, north south east west, look, map, get, inv, talk, help, quit\n");
            continue;
        }

        if (strcmp(input, "look") == 0) {
            print_location(&game);
            continue;
        }

        if (strcmp(input, "map") == 0) {
            print_map(&game);
            continue;
        }

        if (strcmp(input, "inv") == 0 || strcmp(input, "inventory") == 0) {
            print_inventory(&game);
            continue;
        }

        if (strcmp(input, "get") == 0 || strcmp(input, "take") == 0) {
            pickup_item(&game);
            continue;
        }

        if (strcmp(input, "talk") == 0) {
            talk_npc(&game);
            continue;
        }

        if (move_player(&game.player, input)) {
            print_location(&game);
            continue;
        }

        printf("Unknown command: '%s'\n", input);
        printf("Try: n s e w, look, map, get, inv, talk, help, quit\n");
    }

    return 0;
}
