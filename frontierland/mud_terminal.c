#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAP_W 5
#define MAP_H 5
#define INPUT_LEN 128

typedef struct {
    int x;
    int y;
} player_t;

static const char *room_name(int x, int y) {
    if (x == 2 && y == 2) {
        return "Town Crossroads";
    }
    if (x == 0 && y == 0) {
        return "Northwest Ridge";
    }
    if (x == 4 && y == 0) {
        return "Northeast Watch";
    }
    if (x == 0 && y == 4) {
        return "Southwest Flats";
    }
    if (x == 4 && y == 4) {
        return "Southeast Gate";
    }
    return "Dust Trail";
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
    printf("Commands: n s e w, look, map, help, quit\n\n");
}

static void print_location(const player_t *p) {
    printf("You are at [%d,%d] - %s\n", p->x, p->y, room_name(p->x, p->y));

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

static void print_map(const player_t *p) {
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
    player_t player;

    player.x = 2;
    player.y = 2;

    print_intro();
    print_location(&player);

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
            printf("Commands: n s e w, north south east west, look, map, help, quit\n");
            continue;
        }

        if (strcmp(input, "look") == 0) {
            print_location(&player);
            continue;
        }

        if (strcmp(input, "map") == 0) {
            print_map(&player);
            continue;
        }

        if (move_player(&player, input)) {
            print_location(&player);
            continue;
        }

        printf("Unknown command: '%s'\n", input);
        printf("Try: n s e w, look, map, help, quit\n");
    }

    return 0;
}
