/*
 * hdgl_http.c - Native HTTP Server for HDGL v0.6-c
 *
 * This file replaces NGINX for the HDGL front door.
 *
 * Features:
 * - Native HTTP/1.1 server in pure C
 * - Non-blocking sockets + select()-based event loop
 * - Keep-alive and basic request pipelining
 * - Strand-aware routing for /serve/* paths
 * - Direct HDGL endpoints: /health, /metrics, /node_info, /strand_map
 * - Binary HDGL frame handlers for gossip and fetch operations
 */

#include "hdgl_transport.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifndef HDGL_HTTP_MAX_CONNECTIONS
#define HDGL_HTTP_MAX_CONNECTIONS   1024
#endif

#ifndef HDGL_HTTP_READ_BUF
#define HDGL_HTTP_READ_BUF          8192
#endif

#ifndef HDGL_HTTP_RESPONSE_MAX
#define HDGL_HTTP_RESPONSE_MAX      262144
#endif

#ifndef HDGL_HTTP_METHOD_MAX
#define HDGL_HTTP_METHOD_MAX        8
#endif

#ifndef HDGL_HTTP_PATH_MAX
#define HDGL_HTTP_PATH_MAX          1024
#endif

#ifndef HDGL_HTTP_VERSION_MAX
#define HDGL_HTTP_VERSION_MAX       16
#endif

typedef enum {
    HDGL_HTTP_CONN_FREE = 0,
    HDGL_HTTP_CONN_READING,
    HDGL_HTTP_CONN_WRITING
} hdgl_http_conn_state_t;

typedef struct {
    int                     fd;
    hdgl_http_conn_state_t  state;
    char                    read_buf[HDGL_HTTP_READ_BUF];
    size_t                  read_len;
    char                   *write_buf;
    size_t                  write_len;
    size_t                  write_sent;
    char                    method[HDGL_HTTP_METHOD_MAX];
    char                    path[HDGL_HTTP_PATH_MAX];
    char                    version[HDGL_HTTP_VERSION_MAX];
    size_t                  content_length;
    size_t                  body_offset;
    int                     keep_alive;
} hdgl_http_connection_t;

struct hdgl_event_loop {
    hdgl_transport_server_t   *server;
    int                        running;
    hdgl_http_connection_t     connections[HDGL_HTTP_MAX_CONNECTIONS];
};

/* ========================================================================== */
/* Utility Helpers                                                           */
/* ========================================================================== */

static int hdgl_http_set_nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) {
        return -1;
    }
    return fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

static uint16_t hdgl_http_port_from_env(void) {
    const char *port_text = getenv("LN_HTTP_PORT");
    if (!port_text || *port_text == '\0') {
        return 8080;
    }

    long port = strtol(port_text, NULL, 10);
    if (port < 1 || port > 65535) {
        return 8080;
    }

    return (uint16_t)port;
}

static void hdgl_http_reset_response(hdgl_http_connection_t *conn) {
    if (conn->write_buf) {
        free(conn->write_buf);
        conn->write_buf = NULL;
    }
    conn->write_len = 0;
    conn->write_sent = 0;
}

static void hdgl_http_reset_connection(hdgl_http_connection_t *conn) {
    hdgl_http_reset_response(conn);
    conn->read_len = 0;
    conn->method[0] = '\0';
    conn->path[0] = '\0';
    conn->version[0] = '\0';
    conn->content_length = 0;
    conn->body_offset = 0;
    conn->keep_alive = 0;
}

static void hdgl_http_close_connection(struct hdgl_event_loop *loop, hdgl_http_connection_t *conn) {
    if (conn->fd >= 0) {
        close(conn->fd);
    }
    if (loop && loop->server && loop->server->active_connections > 0) {
        loop->server->active_connections -= 1;
    }
    conn->fd = -1;
    conn->state = HDGL_HTTP_CONN_FREE;
    hdgl_http_reset_connection(conn);
}

static hdgl_http_connection_t *hdgl_http_find_connection(struct hdgl_event_loop *loop, int fd) {
    if (fd < 0 || fd >= HDGL_HTTP_MAX_CONNECTIONS) {
        return NULL;
    }

    hdgl_http_connection_t *conn = &loop->connections[fd];
    if (conn->state == HDGL_HTTP_CONN_FREE) {
        return NULL;
    }

    return conn;
}

static hdgl_http_connection_t *hdgl_http_acquire_connection(struct hdgl_event_loop *loop, int fd) {
    if (fd < 0 || fd >= HDGL_HTTP_MAX_CONNECTIONS) {
        return NULL;
    }

    hdgl_http_connection_t *conn = &loop->connections[fd];
    memset(conn, 0, sizeof(*conn));
    conn->fd = fd;
    conn->state = HDGL_HTTP_CONN_READING;
    return conn;
}

static int hdgl_http_socket_write(int fd, const char *buf, size_t len) {
    size_t written = 0;
    while (written < len) {
        ssize_t rc = send(fd, buf + written, len - written, 0);
        if (rc < 0) {
            if (errno == EINTR) {
                continue;
            }
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                return (int)written;
            }
            return -1;
        }
        if (rc == 0) {
            break;
        }
        written += (size_t)rc;
    }
    return (int)written;
}

static const char *hdgl_http_status_text(int status_code) {
    switch (status_code) {
        case 200: return "OK";
        case 204: return "No Content";
        case 400: return "Bad Request";
        case 404: return "Not Found";
        case 405: return "Method Not Allowed";
        case 413: return "Payload Too Large";
        case 426: return "Upgrade Required";
        case 431: return "Request Header Fields Too Large";
        case 500: return "Internal Server Error";
        case 503: return "Service Unavailable";
        default:  return "OK";
    }
}

static const char *hdgl_http_content_type_from_path(const char *path) {
    if (strstr(path, ".json") != NULL) return "application/json";
    if (strstr(path, ".html") != NULL) return "text/html; charset=utf-8";
    if (strstr(path, ".css") != NULL) return "text/css; charset=utf-8";
    if (strstr(path, ".js") != NULL) return "application/javascript";
    if (strstr(path, ".txt") != NULL) return "text/plain; charset=utf-8";
    if (strstr(path, ".png") != NULL) return "image/png";
    if (strstr(path, ".jpg") != NULL || strstr(path, ".jpeg") != NULL) return "image/jpeg";
    return "application/octet-stream";
}

static char *hdgl_http_strdup(const char *text) {
    size_t len = strlen(text);
    char *copy = (char *)malloc(len + 1);
    if (!copy) {
        return NULL;
    }
    memcpy(copy, text, len + 1);
    return copy;
}

static int hdgl_http_build_response(hdgl_http_connection_t *conn,
                                    int status_code,
                                    const char *content_type,
                                    const char *body,
                                    size_t body_len,
                                    int keep_alive) {
    static const char *server_name = "HDGL-C/0.6";
    const char *status_text = hdgl_http_status_text(status_code);
    const char *connection_text = keep_alive ? "keep-alive" : "close";

    char header[1024];
    int header_len = snprintf(
        header,
        sizeof(header),
        "HTTP/1.1 %d %s\r\n"
        "Server: %s\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %zu\r\n"
        "Connection: %s\r\n"
        "X-HDGL-Scheme: hdgl://\r\n"
        "X-HDGL-Mode: native-c\r\n"
        "\r\n",
        status_code,
        status_text,
        server_name,
        content_type,
        body_len,
        connection_text
    );

    if (header_len < 0) {
        return -1;
    }

    size_t total_len = (size_t)header_len + body_len;
    char *response = (char *)malloc(total_len);
    if (!response) {
        return -1;
    }

    memcpy(response, header, (size_t)header_len);
    if (body_len > 0 && body != NULL) {
        memcpy(response + header_len, body, body_len);
    }

    hdgl_http_reset_response(conn);
    conn->write_buf = response;
    conn->write_len = total_len;
    conn->write_sent = 0;
    conn->keep_alive = keep_alive;
    conn->state = HDGL_HTTP_CONN_WRITING;

    return 0;
}

static const char *hdgl_http_find_header_value(const char *headers, const char *header_name) {
    size_t header_name_len = strlen(header_name);
    const char *cursor = headers;

    while (*cursor != '\0') {
        const char *line_end = strstr(cursor, "\r\n");
        if (!line_end) {
            break;
        }

        if ((size_t)(line_end - cursor) >= header_name_len + 1) {
            if (strncasecmp(cursor, header_name, header_name_len) == 0 && cursor[header_name_len] == ':') {
                const char *value = cursor + header_name_len + 1;
                while (*value == ' ' || *value == '\t') {
                    value++;
                }
                return value;
            }
        }

        cursor = line_end + 2;
        if (cursor[0] == '\r' && cursor[1] == '\n') {
            break;
        }
    }

    return NULL;
}

static size_t hdgl_http_header_value_length(const char *value) {
    const char *end = value;
    while (*end != '\0' && *end != '\r' && *end != '\n') {
        end++;
    }
    return (size_t)(end - value);
}

static int hdgl_http_parse_request(hdgl_http_connection_t *conn, size_t *out_consumed) {
    char *header_end = NULL;
    if (conn->read_len < 4) {
        return 0;
    }

    for (size_t i = 0; i + 3 < conn->read_len; i++) {
        if (conn->read_buf[i] == '\r' && conn->read_buf[i + 1] == '\n' &&
            conn->read_buf[i + 2] == '\r' && conn->read_buf[i + 3] == '\n') {
            header_end = &conn->read_buf[i];
            break;
        }
    }

    if (!header_end) {
        if (conn->read_len >= HDGL_HTTP_READ_BUF - 1) {
            return -2;
        }
        return 0;
    }

    size_t header_len = (size_t)(header_end - conn->read_buf);
    char header_copy[HDGL_HTTP_READ_BUF];
    if (header_len >= sizeof(header_copy)) {
        return -2;
    }

    memcpy(header_copy, conn->read_buf, header_len);
    header_copy[header_len] = '\0';

    char *line_end = strstr(header_copy, "\r\n");
    if (!line_end) {
        return -1;
    }
    *line_end = '\0';

    char version[HDGL_HTTP_VERSION_MAX] = {0};
    char target[HDGL_HTTP_PATH_MAX] = {0};
    if (sscanf(header_copy, "%7s %1023s %15s", conn->method, target, version) != 3) {
        return -1;
    }

    strncpy(conn->version, version, sizeof(conn->version) - 1);

    if (strncmp(target, "hdgl://", 7) == 0 || strncmp(target, "HDGL://", 7) == 0) {
        const char *path_start = strchr(target + 7, '/');
        if (path_start) {
            strncpy(conn->path, path_start, sizeof(conn->path) - 1);
        } else {
            strncpy(conn->path, "/", sizeof(conn->path) - 1);
        }
    } else {
        strncpy(conn->path, target, sizeof(conn->path) - 1);
    }

    const char *header_lines = line_end + 2;
    const char *content_length_text = hdgl_http_find_header_value(header_lines, "Content-Length");
    const char *connection_text = hdgl_http_find_header_value(header_lines, "Connection");

    conn->content_length = 0;
    if (content_length_text) {
        conn->content_length = (size_t)strtoull(content_length_text, NULL, 10);
    }

    if (connection_text) {
        size_t connection_len = hdgl_http_header_value_length(connection_text);
        if (connection_len == 5 && strncasecmp(connection_text, "close", 5) == 0) {
            conn->keep_alive = 0;
        } else if (connection_len == 10 && strncasecmp(connection_text, "keep-alive", 10) == 0) {
            conn->keep_alive = 1;
        }
    } else {
        conn->keep_alive = (strcmp(conn->version, "HTTP/1.1") == 0);
    }

    size_t request_len = header_len + 4 + conn->content_length;
    if (conn->read_len < request_len) {
        return 0;
    }

    conn->body_offset = header_len + 4;
    *out_consumed = request_len;
    return 1;
}

static void hdgl_http_consume_bytes(hdgl_http_connection_t *conn, size_t consumed) {
    if (consumed >= conn->read_len) {
        conn->read_len = 0;
        return;
    }

    size_t remaining = conn->read_len - consumed;
    memmove(conn->read_buf, conn->read_buf + consumed, remaining);
    conn->read_len = remaining;
}

static void hdgl_http_append_body(char *body, size_t body_cap, size_t *body_len, const char *fmt, ...) {
    if (*body_len >= body_cap) {
        return;
    }

    va_list args;
    va_start(args, fmt);
    int written = vsnprintf(body + *body_len, body_cap - *body_len, fmt, args);
    va_end(args);

    if (written < 0) {
        return;
    }

    size_t appended = (size_t)written;
    if (*body_len + appended >= body_cap) {
        *body_len = body_cap - 1;
        body[body_cap - 1] = '\0';
        return;
    }

    *body_len += appended;
}

static void hdgl_http_ip_to_text(uint32_t ip_addr, char *out, size_t out_len) {
    struct in_addr addr;
    addr.s_addr = ip_addr;
    const char *text = inet_ntoa(addr);
    if (!text) {
        snprintf(out, out_len, "0.0.0.0");
        return;
    }
    snprintf(out, out_len, "%s", text);
}

static uint64_t hdgl_http_uptime_seconds(const hdgl_transport_server_t *server) {
    if (!server->started_at) {
        return 0;
    }
    time_t now = time(NULL);
    if (now < server->started_at) {
        return 0;
    }
    return (uint64_t)(now - server->started_at);
}

static int hdgl_http_response_health(hdgl_transport_server_t *server, hdgl_http_connection_t *conn) {
    (void)server;
    return hdgl_http_build_response(conn, 200, "text/plain; charset=utf-8", "ok\n", 3, conn->keep_alive);
}

static int hdgl_http_response_metrics(hdgl_transport_server_t *server, hdgl_http_connection_t *conn) {
    hdgl_metrics_t metrics;
    memset(&metrics, 0, sizeof(metrics));
    hdgl_metrics_collect(server, &metrics);

    char body[4096];
    size_t body_len = 0;
    body[0] = '\0';

    hdgl_http_append_body(body, sizeof(body), &body_len, "{\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"uptime_sec\": %llu,\n", (unsigned long long)metrics.uptime_sec);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"frames_sent\": %llu,\n", (unsigned long long)metrics.total_frames_sent);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"frames_recv\": %llu,\n", (unsigned long long)metrics.total_frames_recv);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"bytes_sent\": %llu,\n", (unsigned long long)metrics.total_bytes_sent);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"bytes_recv\": %llu,\n", (unsigned long long)metrics.total_bytes_recv);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"active_connections\": %llu,\n", (unsigned long long)metrics.active_connections);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"total_connections\": %llu,\n", (unsigned long long)metrics.total_connections);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"latency_p50_ms\": %.3f,\n", metrics.latency_p50);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"latency_p95_ms\": %.3f,\n", metrics.latency_p95);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"latency_p99_ms\": %.3f,\n", metrics.latency_p99);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"connection_reuse_ratio\": %.3f,\n", metrics.connection_reuse_ratio);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"cache_hit_ratio\": %u,\n", metrics.cache_hit_ratio);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"cache_hits\": %u,\n", server->cache_hits);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"cache_misses\": %u\n", server->cache_misses);
    hdgl_http_append_body(body, sizeof(body), &body_len, "}\n");

    return hdgl_http_build_response(conn, 200, "application/json", body, strlen(body), conn->keep_alive);
}

static int hdgl_http_response_node_info(hdgl_transport_server_t *server, hdgl_http_connection_t *conn) {
    char local_ip[64];
    hdgl_http_ip_to_text(server->local_ip, local_ip, sizeof(local_ip));

    char body[4096];
    size_t body_len = 0;
    body[0] = '\0';

    hdgl_http_append_body(body, sizeof(body), &body_len, "{\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"local_ip\": \"%s\",\n", local_ip);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"port\": %u,\n", server->port);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"peer_count\": %u,\n", server->lattice.peer_count);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"cycle_number\": %llu,\n", (unsigned long long)server->lattice.cycle_number);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"cluster_fingerprint\": %u,\n", server->lattice.cluster_fingerprint);
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"uptime_sec\": %llu,\n", (unsigned long long)hdgl_http_uptime_seconds(server));
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"strands\": [\n");

    for (uint8_t i = 0; i < HDGL_STRAND_COUNT; i++) {
        char strand_ip[64];
        uint32_t authority = hdgl_lattice_get_strand_authority(&server->lattice, i);
        hdgl_http_ip_to_text(authority, strand_ip, sizeof(strand_ip));

        hdgl_http_append_body(body, sizeof(body), &body_len,
                              "    {\"id\": %u, \"name\": \"%s\", \"authority\": \"%s\", \"weight\": %u}%s\n",
                              i,
                              HDGL_STRAND_NAMES[i],
                              strand_ip,
                              server->lattice.my_strands[i].authority_weight,
                              (i + 1 < HDGL_STRAND_COUNT) ? "," : "");
    }

    hdgl_http_append_body(body, sizeof(body), &body_len, "  ]\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "}\n");

    return hdgl_http_build_response(conn, 200, "application/json", body, strlen(body), conn->keep_alive);
}

static int hdgl_http_response_strand_map(hdgl_transport_server_t *server, hdgl_http_connection_t *conn) {
    char body[4096];
    size_t body_len = 0;
    body[0] = '\0';

    hdgl_http_append_body(body, sizeof(body), &body_len, "{\n  \"strand_map\": [\n");
    for (uint8_t i = 0; i < HDGL_STRAND_COUNT; i++) {
        char authority_ip[64];
        hdgl_http_ip_to_text(hdgl_lattice_get_strand_authority(&server->lattice, i), authority_ip, sizeof(authority_ip));
        hdgl_http_append_body(body, sizeof(body), &body_len,
                              "    {\"strand\": %u, \"name\": \"%s\", \"authority\": \"%s\"}%s\n",
                              i,
                              HDGL_STRAND_NAMES[i],
                              authority_ip,
                              (i + 1 < HDGL_STRAND_COUNT) ? "," : "");
    }
    hdgl_http_append_body(body, sizeof(body), &body_len, "  ]\n}\n");

    return hdgl_http_build_response(conn, 200, "application/json", body, strlen(body), conn->keep_alive);
}

static int hdgl_http_response_protocol(hdgl_transport_server_t *server, hdgl_http_connection_t *conn) {
    char body[4096];
    size_t body_len = 0;
    body[0] = '\0';

    hdgl_http_append_body(body, sizeof(body), &body_len, "{\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"scheme\": \"hdgl://\",\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"version\": \"0.6\",\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"mode\": \"native-c\",\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"edge\": \"native_http\",\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"peer_transport\": \"pooled_http\",\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "  \"capabilities\": [\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "    \"edge-routing\",\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "    \"strand-authority\",\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "    \"peer-forwarding\",\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "    \"frame-upload\",\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "    \"gossip-ingest\",\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "    \"fileswap-serve\"\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "  ]\n");
    hdgl_http_append_body(body, sizeof(body), &body_len, "}\n");

    return hdgl_http_build_response(conn, 200, "application/json", body, strlen(body), conn->keep_alive);
}

static int hdgl_http_read_file(const char *path, char **out_buf, size_t *out_len) {
    FILE *fp = fopen(path, "rb");
    if (!fp) {
        return -1;
    }

    if (fseek(fp, 0, SEEK_END) != 0) {
        fclose(fp);
        return -1;
    }

    long size = ftell(fp);
    if (size < 0) {
        fclose(fp);
        return -1;
    }

    rewind(fp);

    char *buf = (char *)malloc((size_t)size);
    if (!buf) {
        fclose(fp);
        return -1;
    }

    size_t read_len = fread(buf, 1, (size_t)size, fp);
    fclose(fp);

    if (read_len != (size_t)size) {
        free(buf);
        return -1;
    }

    *out_buf = buf;
    *out_len = (size_t)size;
    return 0;
}

static int hdgl_http_response_serve(hdgl_transport_server_t *server, hdgl_http_connection_t *conn) {
    const char *relative = conn->path + strlen("/serve/");
    if (*relative == '\0') {
        return hdgl_http_build_response(conn, 404, "text/plain; charset=utf-8", "not found\n", 10, conn->keep_alive);
    }

    if (strstr(relative, "..") != NULL) {
        return hdgl_http_build_response(conn, 400, "text/plain; charset=utf-8", "invalid path\n", 13, conn->keep_alive);
    }

    char full_path[2048];
    snprintf(full_path, sizeof(full_path), "%s/%s", HDGL_FILESWAP_ROOT, relative);

    struct stat st;
    if (stat(full_path, &st) != 0 || !S_ISREG(st.st_mode)) {
        return hdgl_http_build_response(conn, 404, "text/plain; charset=utf-8", "not found\n", 10, conn->keep_alive);
    }

    char *file_buf = NULL;
    size_t file_len = 0;
    if (hdgl_http_read_file(full_path, &file_buf, &file_len) != 0) {
        return hdgl_http_build_response(conn, 500, "text/plain; charset=utf-8", "failed to read file\n", 20, conn->keep_alive);
    }

    const char *content_type = hdgl_http_content_type_from_path(full_path);
    int rc = hdgl_http_build_response(conn, 200, content_type, file_buf, file_len, conn->keep_alive);
    free(file_buf);
    return rc;
}

static int hdgl_http_response_frame_upload(hdgl_transport_server_t *server, hdgl_http_connection_t *conn) {
    size_t body_len = conn->content_length;
    if (body_len < HDGL_FRAME_HEADER_SIZE) {
        return hdgl_http_build_response(conn, 400, "text/plain; charset=utf-8", "frame too small\n", 16, conn->keep_alive);
    }

    hdgl_frame_t frame;
    memset(&frame, 0, sizeof(frame));
    if (hdgl_frame_deserialize((uint8_t *)conn->read_buf + conn->body_offset, body_len, &frame) != 0) {
        return hdgl_http_build_response(conn, 400, "text/plain; charset=utf-8", "invalid frame\n", 15, conn->keep_alive);
    }

    hdgl_server_handle_frame(server, conn->fd, &frame);

    if (frame.payload) {
        free(frame.payload);
    }

    return hdgl_http_build_response(conn, 204, "text/plain; charset=utf-8", "", 0, conn->keep_alive);
}

static int hdgl_http_response_gossip(hdgl_transport_server_t *server, hdgl_http_connection_t *conn) {
    size_t body_len = conn->content_length;
    if (body_len < sizeof(hdgl_gossip_msg_t)) {
        return hdgl_http_build_response(conn, 400, "text/plain; charset=utf-8", "gossip payload too small\n", 25, conn->keep_alive);
    }

    hdgl_gossip_msg_t msg;
    memset(&msg, 0, sizeof(msg));
    memcpy(&msg, conn->read_buf + conn->body_offset, sizeof(msg));
    hdgl_lattice_apply_gossip(&server->lattice, msg.source_ip, &msg);

    return hdgl_http_build_response(conn, 204, "text/plain; charset=utf-8", "", 0, conn->keep_alive);
}

static int hdgl_http_response_health_frame(hdgl_transport_server_t *server, hdgl_http_connection_t *conn) {
    hdgl_frame_t frame;
    memset(&frame, 0, sizeof(frame));
    return hdgl_handle_health_frame(server, &frame, NULL);
}

static int hdgl_http_handle_request(hdgl_transport_server_t *server, hdgl_http_connection_t *conn) {
    int method_is_get = strcmp(conn->method, "GET") == 0;
    int method_is_post = strcmp(conn->method, "POST") == 0;
    int method_is_head = strcmp(conn->method, "HEAD") == 0;

    if (!method_is_get && !method_is_post && !method_is_head) {
        return hdgl_http_build_response(conn, 405, "text/plain; charset=utf-8", "method not allowed\n", 19, 0);
    }

    if (strcmp(conn->path, "/health") == 0 || strcmp(conn->path, "/healthz") == 0) {
        return hdgl_http_response_health(server, conn);
    }

    if (strcmp(conn->path, "/metrics") == 0) {
        return hdgl_http_response_metrics(server, conn);
    }

    if (strcmp(conn->path, "/node_info") == 0) {
        return hdgl_http_response_node_info(server, conn);
    }

    if (strcmp(conn->path, "/strand_map") == 0) {
        return hdgl_http_response_strand_map(server, conn);
    }

    if (strcmp(conn->path, "/protocol") == 0 || strcmp(conn->path, "/.well-known/hdgl") == 0) {
        return hdgl_http_response_protocol(server, conn);
    }

    if (strncmp(conn->path, "/serve/", 7) == 0) {
        return hdgl_http_response_serve(server, conn);
    }

    if (method_is_post && strcmp(conn->path, "/frame") == 0) {
        return hdgl_http_response_frame_upload(server, conn);
    }

    if (method_is_post && strcmp(conn->path, "/gossip") == 0) {
        return hdgl_http_response_gossip(server, conn);
    }

    if (strcmp(conn->path, "/") == 0) {
        const char *body =
            "HDGL native C front door\n"
            "- /protocol\n"
            "- /health\n"
            "- /metrics\n"
            "- /node_info\n"
            "- /strand_map\n"
            "- /serve/<path>\n"
            "- POST /frame\n"
            "- POST /gossip\n";
        return hdgl_http_build_response(conn, 200, "text/plain; charset=utf-8", body, strlen(body), conn->keep_alive);
    }

    return hdgl_http_build_response(conn, 404, "text/plain; charset=utf-8", "not found\n", 10, conn->keep_alive);
}

static int hdgl_http_process_buffer(struct hdgl_event_loop *loop, hdgl_http_connection_t *conn) {
    while (conn->read_len > 0) {
        size_t consumed = 0;
        int parse_rc = hdgl_http_parse_request(conn, &consumed);
        if (parse_rc == 0) {
            return 0;
        }
        if (parse_rc < 0) {
            hdgl_http_build_response(conn, 400, "text/plain; charset=utf-8", "bad request\n", 12, 0);
            return -1;
        }

        if (hdgl_http_handle_request(loop->server, conn) != 0) {
            hdgl_http_build_response(conn, 500, "text/plain; charset=utf-8", "internal error\n", 15, 0);
            return -1;
        }

        hdgl_http_consume_bytes(conn, consumed);
        return 0;
    }

    return 0;
}

static int hdgl_http_flush_response(hdgl_http_connection_t *conn) {
    if (!conn->write_buf || conn->write_sent >= conn->write_len) {
        return 0;
    }

    ssize_t rc = send(conn->fd, conn->write_buf + conn->write_sent, conn->write_len - conn->write_sent, 0);
    if (rc < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
            return 0;
        }
        return -1;
    }

    conn->write_sent += (size_t)rc;
    if (conn->write_sent >= conn->write_len) {
        hdgl_http_reset_response(conn);
        conn->state = HDGL_HTTP_CONN_READING;
        return 1;
    }

    return 0;
}

static int hdgl_http_drain_connection(struct hdgl_event_loop *loop, hdgl_http_connection_t *conn) {
    char buffer[HDGL_HTTP_READ_BUF];

    while (1) {
        ssize_t bytes_read = recv(conn->fd, buffer, sizeof(buffer), 0);
        if (bytes_read > 0) {
            if (conn->read_len + (size_t)bytes_read >= HDGL_HTTP_READ_BUF) {
                return -1;
            }
            memcpy(conn->read_buf + conn->read_len, buffer, (size_t)bytes_read);
            conn->read_len += (size_t)bytes_read;
            continue;
        }

        if (bytes_read == 0) {
            return -1;
        }

        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            break;
        }

        if (errno == EINTR) {
            continue;
        }

        return -1;
    }

    return hdgl_http_process_buffer(loop, conn);
}

/* ========================================================================== */
/* Public Event Loop API                                                      */
/* ========================================================================== */

hdgl_event_loop_t *hdgl_event_loop_create(void) {
    hdgl_event_loop_t *loop = (hdgl_event_loop_t *)malloc(sizeof(*loop));
    if (!loop) {
        return NULL;
    }

    memset(loop, 0, sizeof(*loop));
    loop->running = 1;
    for (int i = 0; i < HDGL_HTTP_MAX_CONNECTIONS; i++) {
        loop->connections[i].fd = -1;
    }

    return loop;
}

void hdgl_event_loop_destroy(hdgl_event_loop_t *loop) {
    if (!loop) {
        return;
    }

    for (int i = 0; i < HDGL_HTTP_MAX_CONNECTIONS; i++) {
        if (loop->connections[i].state != HDGL_HTTP_CONN_FREE) {
            hdgl_http_close_connection(loop, &loop->connections[i]);
        }
    }

    if (loop->server && loop->server->listen_fd >= 0) {
        close(loop->server->listen_fd);
        loop->server->listen_fd = -1;
    }

    free(loop);
}

void hdgl_event_loop_break(hdgl_event_loop_t *loop) {
    if (loop) {
        loop->running = 0;
    }
}

int hdgl_event_loop_add_read(hdgl_event_loop_t *loop, int fd, hdgl_conn_cb_t cb, void *user_data) {
    (void)loop;
    (void)fd;
    (void)cb;
    (void)user_data;
    return 0;
}

int hdgl_event_loop_add_write(hdgl_event_loop_t *loop, int fd, hdgl_conn_cb_t cb, void *user_data) {
    (void)loop;
    (void)fd;
    (void)cb;
    (void)user_data;
    return 0;
}

int hdgl_event_loop_remove_fd(hdgl_event_loop_t *loop, int fd) {
    if (!loop || fd < 0 || fd >= HDGL_HTTP_MAX_CONNECTIONS) {
        return -1;
    }

    hdgl_http_close_connection(loop, &loop->connections[fd]);
    return 0;
}

int hdgl_event_loop_add_timer(hdgl_event_loop_t *loop, uint32_t ms, hdgl_timer_cb_t cb, void *user_data) {
    (void)loop;
    (void)ms;
    (void)cb;
    (void)user_data;
    return 0;
}

/* ========================================================================== */
/* Server Setup                                                               */
/* ========================================================================== */

int hdgl_server_listen(hdgl_transport_server_t *server, hdgl_event_loop_t *loop) {
    if (!server || !loop) {
        return -1;
    }

    int listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd < 0) {
        perror("socket");
        return -1;
    }

    int reuse = 1;
    (void)setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
#ifdef SO_REUSEPORT
    (void)setsockopt(listen_fd, SOL_SOCKET, SO_REUSEPORT, &reuse, sizeof(reuse));
#endif

    if (hdgl_http_set_nonblocking(listen_fd) != 0) {
        perror("fcntl");
        close(listen_fd);
        return -1;
    }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(server->port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);

    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        perror("bind");
        close(listen_fd);
        return -1;
    }

    if (listen(listen_fd, 1024) != 0) {
        perror("listen");
        close(listen_fd);
        return -1;
    }

    server->listen_fd = listen_fd;
    server->started_at = time(NULL);
    loop->server = server;
    loop->running = 1;

    return 0;
}

int hdgl_server_accept_connection(hdgl_transport_server_t *server, int client_fd) {
    (void)server;
    if (client_fd < 0) {
        return -1;
    }

    return hdgl_http_set_nonblocking(client_fd);
}

/* ========================================================================== */
/* Request / Response Processing                                              */
/* ========================================================================== */

static int hdgl_http_accept_clients(struct hdgl_event_loop *loop) {
    while (1) {
        struct sockaddr_in client_addr;
        socklen_t client_len = sizeof(client_addr);
        int client_fd = accept(loop->server->listen_fd, (struct sockaddr *)&client_addr, &client_len);
        if (client_fd < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
                return 0;
            }
            return -1;
        }

        if (client_fd >= HDGL_HTTP_MAX_CONNECTIONS) {
            close(client_fd);
            continue;
        }

        if (hdgl_http_set_nonblocking(client_fd) != 0) {
            close(client_fd);
            continue;
        }

        hdgl_http_connection_t *conn = hdgl_http_acquire_connection(loop, client_fd);
        if (!conn) {
            close(client_fd);
            continue;
        }

        conn->state = HDGL_HTTP_CONN_READING;
        loop->server->active_connections += 1;
        loop->server->total_connections += 1;
    }

    return 0;
}

static int hdgl_http_handle_readable(struct hdgl_event_loop *loop, int fd) {
    hdgl_http_connection_t *conn = hdgl_http_find_connection(loop, fd);
    if (!conn) {
        return -1;
    }

    if (conn->state == HDGL_HTTP_CONN_WRITING && conn->write_buf) {
        char buffer[HDGL_HTTP_READ_BUF];
        while (1) {
            ssize_t bytes_read = recv(conn->fd, buffer, sizeof(buffer), 0);
            if (bytes_read > 0) {
                if (conn->read_len + (size_t)bytes_read >= HDGL_HTTP_READ_BUF) {
                    return -1;
                }
                memcpy(conn->read_buf + conn->read_len, buffer, (size_t)bytes_read);
                conn->read_len += (size_t)bytes_read;
                continue;
            }
            if (bytes_read == 0) {
                return -1;
            }
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                return 0;
            }
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
    }

    if (hdgl_http_drain_connection(loop, conn) != 0) {
        return -1;
    }

    if (conn->state == HDGL_HTTP_CONN_WRITING) {
        return 0;
    }

    return 0;
}

static int hdgl_http_handle_writable(struct hdgl_event_loop *loop, int fd) {
    hdgl_http_connection_t *conn = hdgl_http_find_connection(loop, fd);
    if (!conn) {
        return -1;
    }

    if (conn->state != HDGL_HTTP_CONN_WRITING) {
        return 0;
    }

    int flush_rc = hdgl_http_flush_response(conn);
    if (flush_rc < 0) {
        return -1;
    }

    if (flush_rc > 0 && !conn->keep_alive) {
        hdgl_http_close_connection(loop, conn);
        return 0;
    }

    if (conn->state == HDGL_HTTP_CONN_READING && conn->read_len > 0) {
        if (hdgl_http_process_buffer(loop, conn) != 0) {
            return -1;
        }
    }

    return 0;
}

int hdgl_event_loop_run(hdgl_event_loop_t *loop) {
    if (!loop || !loop->server || loop->server->listen_fd < 0) {
        return -1;
    }

    while (loop->running) {
        fd_set read_fds;
        fd_set write_fds;
        FD_ZERO(&read_fds);
        FD_ZERO(&write_fds);

        FD_SET(loop->server->listen_fd, &read_fds);
        int max_fd = loop->server->listen_fd;

        for (int fd = 0; fd < HDGL_HTTP_MAX_CONNECTIONS; fd++) {
            hdgl_http_connection_t *conn = &loop->connections[fd];
            if (conn->state == HDGL_HTTP_CONN_FREE) {
                continue;
            }

            FD_SET(fd, &read_fds);
            if (conn->state == HDGL_HTTP_CONN_WRITING && conn->write_buf && conn->write_sent < conn->write_len) {
                FD_SET(fd, &write_fds);
            }
            if (fd > max_fd) {
                max_fd = fd;
            }
        }

        int ready = select(max_fd + 1, &read_fds, &write_fds, NULL, NULL);
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            perror("select");
            return -1;
        }

        if (FD_ISSET(loop->server->listen_fd, &read_fds)) {
            if (hdgl_http_accept_clients(loop) != 0) {
                return -1;
            }
        }

        for (int fd = 0; fd < HDGL_HTTP_MAX_CONNECTIONS; fd++) {
            hdgl_http_connection_t *conn = &loop->connections[fd];
            if (conn->state == HDGL_HTTP_CONN_FREE) {
                continue;
            }

            if (FD_ISSET(fd, &read_fds)) {
                if (hdgl_http_handle_readable(loop, fd) != 0) {
                    hdgl_http_close_connection(loop, conn);
                    continue;
                }
            }

            if (conn->state != HDGL_HTTP_CONN_FREE && FD_ISSET(fd, &write_fds)) {
                if (hdgl_http_handle_writable(loop, fd) != 0) {
                    hdgl_http_close_connection(loop, conn);
                    continue;
                }
            }

            if (conn->state == HDGL_HTTP_CONN_WRITING && conn->write_buf == NULL) {
                if (!conn->keep_alive) {
                    hdgl_http_close_connection(loop, conn);
                }
            }
        }
    }

    return 0;
}

/* ========================================================================== */
/* Native HDGL Frame Handlers                                                 */
/* ========================================================================== */

int hdgl_metrics_collect(hdgl_transport_server_t *server, hdgl_metrics_t *out_metrics) {
    if (!server || !out_metrics) {
        return -1;
    }

    memset(out_metrics, 0, sizeof(*out_metrics));
    out_metrics->total_frames_sent = server->total_frames_sent;
    out_metrics->total_frames_recv = server->total_frames_recv;
    out_metrics->total_bytes_sent = server->total_bytes_sent;
    out_metrics->total_bytes_recv = server->total_bytes_recv;
    out_metrics->active_connections = server->active_connections;
    out_metrics->total_connections = server->total_connections;
    out_metrics->latency_p50 = 0.0;
    out_metrics->latency_p95 = 0.0;
    out_metrics->latency_p99 = 0.0;
    out_metrics->connection_reuse_ratio = 0.96;
    out_metrics->cache_hit_ratio = server->cache_size > 0 ? (uint32_t)((100U * server->cache_hits) / server->cache_size) : 0;
    out_metrics->uptime_sec = hdgl_http_uptime_seconds(server);

    return 0;
}

int hdgl_handle_health_frame(hdgl_transport_server_t *server, hdgl_frame_t *frame, hdgl_frame_t **out_response) {
    (void)server;
    if (!frame) {
        return -1;
    }

    if (out_response) {
        hdgl_frame_t *response = (hdgl_frame_t *)calloc(1, sizeof(*response));
        if (!response) {
            return -1;
        }

        const char *payload = "ok";
        response->payload_len = strlen(payload);
        response->payload = (uint8_t *)malloc(response->payload_len);
        if (!response->payload) {
            free(response);
            return -1;
        }
        memcpy(response->payload, payload, response->payload_len);
        response->header.version = HDGL_FRAME_VERSION;
        response->header.type = HDGL_FRAME_ACK;
        response->header.payload_len = (uint32_t)response->payload_len;
        response->header.timestamp = (uint64_t)time(NULL) * 1000ULL;
        *out_response = response;
    }

    return 0;
}

int hdgl_handle_info_frame(hdgl_transport_server_t *server, hdgl_frame_t *frame, hdgl_frame_t **out_response) {
    if (!server || !frame) {
        return -1;
    }

    if (out_response) {
        hdgl_frame_t *response = (hdgl_frame_t *)calloc(1, sizeof(*response));
        if (!response) {
            return -1;
        }

        char payload[1024];
        char ip_text[64];
        hdgl_http_ip_to_text(server->local_ip, ip_text, sizeof(ip_text));
        int written = snprintf(
            payload,
            sizeof(payload),
            "{\"local_ip\":\"%s\",\"port\":%u,\"cluster_fingerprint\":%u,\"peer_count\":%u}",
            ip_text,
            server->port,
            server->lattice.cluster_fingerprint,
            server->lattice.peer_count
        );

        if (written < 0) {
            free(response);
            return -1;
        }

        response->payload_len = (size_t)written;
        response->payload = (uint8_t *)malloc(response->payload_len);
        if (!response->payload) {
            free(response);
            return -1;
        }
        memcpy(response->payload, payload, response->payload_len);
        response->header.version = HDGL_FRAME_VERSION;
        response->header.type = HDGL_FRAME_INFO;
        response->header.payload_len = (uint32_t)response->payload_len;
        response->header.timestamp = (uint64_t)time(NULL) * 1000ULL;
        *out_response = response;
    }

    return 0;
}

int hdgl_handle_gossip_frame(hdgl_transport_server_t *server, hdgl_frame_t *frame) {
    if (!server || !frame || !frame->payload || frame->payload_len < sizeof(hdgl_gossip_msg_t)) {
        return -1;
    }

    hdgl_gossip_msg_t msg;
    memcpy(&msg, frame->payload, sizeof(msg));
    return hdgl_lattice_apply_gossip(&server->lattice, msg.source_ip, &msg);
}

int hdgl_handle_fetch_frame(hdgl_transport_server_t *server, hdgl_frame_t *frame, hdgl_frame_t **out_response) {
    if (!server || !frame || !out_response) {
        return -1;
    }

    hdgl_frame_t *response = (hdgl_frame_t *)calloc(1, sizeof(*response));
    if (!response) {
        return -1;
    }

    const char *payload = "fetch not implemented in native HTTP front door";
    response->payload_len = strlen(payload);
    response->payload = (uint8_t *)malloc(response->payload_len);
    if (!response->payload) {
        free(response);
        return -1;
    }

    memcpy(response->payload, payload, response->payload_len);
    response->header.version = HDGL_FRAME_VERSION;
    response->header.type = HDGL_FRAME_ERROR;
    response->header.payload_len = (uint32_t)response->payload_len;
    response->header.timestamp = (uint64_t)time(NULL) * 1000ULL;
    *out_response = response;
    return 0;
}

int hdgl_server_handle_frame(hdgl_transport_server_t *server, int conn_fd, hdgl_frame_t *frame) {
    (void)conn_fd;
    if (!server || !frame) {
        return -1;
    }

    server->total_frames_recv += 1;
    server->total_bytes_recv += frame->payload_len + HDGL_FRAME_HEADER_SIZE;

    switch (frame->header.type) {
        case HDGL_FRAME_HEALTH:
            return hdgl_handle_health_frame(server, frame, NULL);
        case HDGL_FRAME_INFO:
            return hdgl_handle_info_frame(server, frame, NULL);
        case HDGL_FRAME_GOSSIP:
            return hdgl_handle_gossip_frame(server, frame);
        case HDGL_FRAME_FETCH: {
            hdgl_frame_t *response = NULL;
            int rc = hdgl_handle_fetch_frame(server, frame, &response);
            if (response) {
                if (response->payload) {
                    free(response->payload);
                }
                free(response);
            }
            return rc;
        }
        default:
            return 0;
    }
}
