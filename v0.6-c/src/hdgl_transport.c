/*
 * hdgl_transport.c - Native HDGL Peer Transport
 *
 * Peer communication for v0.6-c uses HTTP/1.1 over pooled sockets.
 * This keeps the external edge native while preserving a simple
 * request/response model for peer forwarding and health traffic.
 */

#include "hdgl_transport.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#ifndef HDGL_CLIENT_READ_BUF
#define HDGL_CLIENT_READ_BUF 16384
#endif

#ifndef HDGL_CLIENT_HTTP_PORT_DEFAULT
#define HDGL_CLIENT_HTTP_PORT_DEFAULT 8080
#endif

static hdgl_connection_pool_t *hdgl_transport_find_pool(hdgl_transport_server_t *server, uint32_t peer_ip) {
    if (!server || !server->peer_pools) {
        return NULL;
    }

    for (uint32_t i = 0; i < server->pool_count; i++) {
        if (server->peer_pools[i].peer_ip == peer_ip) {
            return &server->peer_pools[i];
        }
    }

    return NULL;
}

static hdgl_connection_pool_t *hdgl_transport_get_or_create_pool(hdgl_transport_server_t *server, uint32_t peer_ip) {
    hdgl_connection_pool_t *pool = hdgl_transport_find_pool(server, peer_ip);
    if (pool) {
        return pool;
    }

    hdgl_connection_pool_t *resized = (hdgl_connection_pool_t *)realloc(
        server->peer_pools,
        (server->pool_count + 1) * sizeof(*server->peer_pools)
    );
    if (!resized) {
        return NULL;
    }

    server->peer_pools = resized;
    pool = &server->peer_pools[server->pool_count++];
    memset(pool, 0, sizeof(*pool));
    pool->peer_ip = peer_ip;
    pool->created_at = time(NULL);
    return pool;
}

static int hdgl_transport_socket_nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) {
        return -1;
    }
    return fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

static int hdgl_transport_socket_connect(uint32_t peer_ip, uint16_t peer_port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        return -1;
    }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(peer_port);
    addr.sin_addr.s_addr = peer_ip;

    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        close(fd);
        return -1;
    }

    return fd;
}

static ssize_t hdgl_transport_write_all(int fd, const uint8_t *buf, size_t len) {
    size_t written = 0;
    while (written < len) {
        ssize_t rc = send(fd, buf + written, len - written, 0);
        if (rc < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (rc == 0) {
            break;
        }
        written += (size_t)rc;
    }
    return (ssize_t)written;
}

static ssize_t hdgl_transport_read_all(int fd, uint8_t *buf, size_t len) {
    size_t read_total = 0;
    while (read_total < len) {
        ssize_t rc = recv(fd, buf + read_total, len - read_total, 0);
        if (rc < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        if (rc == 0) {
            break;
        }
        read_total += (size_t)rc;
    }
    return (ssize_t)read_total;
}

static int hdgl_transport_send_http_frame(hdgl_transport_server_t *server,
                                          uint32_t peer_ip,
                                          uint16_t peer_port,
                                          hdgl_frame_t *frame,
                                          hdgl_frame_t **out_response) {
    if (!server || !frame) {
        return -1;
    }

    if (hdgl_hmac_sign_frame(frame, server->cluster_secret, server->secret_len) != 0) {
        return -1;
    }

    uint8_t *frame_buf = NULL;
    size_t frame_len = 0;
    if (hdgl_frame_serialize(frame, &frame_buf, &frame_len) != 0) {
        return -1;
    }

    hdgl_connection_pool_t *pool = hdgl_transport_get_or_create_pool(server, peer_ip);
    if (!pool) {
        free(frame_buf);
        return -1;
    }

    int fd = -1;
    for (uint32_t i = 0; i < pool->conn_count; i++) {
        hdgl_pooled_conn_t *conn = &pool->connections[i];
        if (!conn->is_valid) {
            continue;
        }
        if (conn->peer_port != peer_port) {
            continue;
        }
        if ((time(NULL) - conn->last_used) > (time_t)HDGL_KEEP_ALIVE_TTL) {
            close(conn->fd);
            conn->is_valid = 0;
            continue;
        }
        fd = conn->fd;
        conn->request_count += 1;
        conn->last_used = time(NULL);
        pool->total_reused += 1;
        break;
    }

    if (fd < 0) {
        fd = hdgl_transport_socket_connect(peer_ip, peer_port);
        if (fd < 0) {
            free(frame_buf);
            return -1;
        }

        if (pool->conn_count < HDGL_MAX_POOL_SIZE) {
            hdgl_pooled_conn_t *slot = &pool->connections[pool->conn_count++];
            memset(slot, 0, sizeof(*slot));
            slot->fd = fd;
            slot->peer_ip = peer_ip;
            slot->peer_port = peer_port;
            slot->created_at = time(NULL);
            slot->last_used = slot->created_at;
            slot->request_count = 1;
            slot->is_valid = 1;
            pool->total_new += 1;
        }
    }

    char peer_text[64];
    struct in_addr peer_addr;
    peer_addr.s_addr = peer_ip;
    snprintf(peer_text, sizeof(peer_text), "%s", inet_ntoa(peer_addr));

    char header[512];
    int header_len = snprintf(
        header,
        sizeof(header),
        "POST hdgl://%s/frame HTTP/1.1\r\n"
        "Host: %u\r\n"
        "Content-Type: application/octet-stream\r\n"
        "Content-Length: %zu\r\n"
        "Connection: keep-alive\r\n"
        "X-HDGL-Scheme: hdgl://\r\n"
        "X-HDGL-Mode: native-c\r\n"
        "\r\n",
        peer_text,
        peer_ip,
        frame_len
    );
    if (header_len < 0) {
        free(frame_buf);
        return -1;
    }

    if (hdgl_transport_write_all(fd, (const uint8_t *)header, (size_t)header_len) < 0 ||
        hdgl_transport_write_all(fd, frame_buf, frame_len) < 0) {
        free(frame_buf);
        if (out_response) {
            *out_response = NULL;
        }
        return -1;
    }

    /* Read response status and body. We keep this intentionally simple. */
    char response_buf[HDGL_CLIENT_READ_BUF];
    ssize_t response_len = recv(fd, response_buf, sizeof(response_buf) - 1, 0);
    if (response_len < 0) {
        free(frame_buf);
        return -1;
    }
    response_buf[response_len] = '\0';

    if (out_response) {
        hdgl_frame_t *response = (hdgl_frame_t *)calloc(1, sizeof(*response));
        if (!response) {
            free(frame_buf);
            return -1;
        }

        response->header.version = HDGL_FRAME_VERSION;
        response->header.type = HDGL_FRAME_ACK;
        response->header.timestamp = (uint64_t)time(NULL) * 1000ULL;
        response->payload_len = 0;
        response->payload = NULL;
        *out_response = response;
    }

    free(frame_buf);
    return 0;
}

int hdgl_pool_get_connection(hdgl_connection_pool_t *pool, int *out_fd) {
    if (!pool || !out_fd) {
        return -1;
    }

    for (uint32_t i = 0; i < pool->conn_count; i++) {
        hdgl_pooled_conn_t *conn = &pool->connections[i];
        if (!conn->is_valid) {
            continue;
        }
        if ((time(NULL) - conn->last_used) > (time_t)HDGL_KEEP_ALIVE_TTL) {
            close(conn->fd);
            conn->is_valid = 0;
            continue;
        }
        conn->last_used = time(NULL);
        conn->request_count += 1;
        *out_fd = conn->fd;
        pool->total_reused += 1;
        return 0;
    }

    *out_fd = -1;
    return -1;
}

void hdgl_pool_return_connection(hdgl_connection_pool_t *pool, int fd) {
    if (!pool || fd < 0) {
        return;
    }

    for (uint32_t i = 0; i < pool->conn_count; i++) {
        hdgl_pooled_conn_t *conn = &pool->connections[i];
        if (conn->fd == fd) {
            conn->last_used = time(NULL);
            conn->is_valid = 1;
            return;
        }
    }
}

void hdgl_pool_invalidate_connection(hdgl_connection_pool_t *pool, int fd) {
    if (!pool || fd < 0) {
        return;
    }

    for (uint32_t i = 0; i < pool->conn_count; i++) {
        hdgl_pooled_conn_t *conn = &pool->connections[i];
        if (conn->fd == fd) {
            close(conn->fd);
            conn->is_valid = 0;
            conn->error_count += 1;
            return;
        }
    }
}

int hdgl_client_connect_to_peer(hdgl_transport_server_t *server, uint32_t peer_ip, uint16_t peer_port) {
    if (!server) {
        return -1;
    }

    hdgl_connection_pool_t *pool = hdgl_transport_get_or_create_pool(server, peer_ip);
    if (!pool) {
        return -1;
    }

    int fd = -1;
    if (hdgl_pool_get_connection(pool, &fd) == 0) {
        return fd;
    }

    fd = hdgl_transport_socket_connect(peer_ip, peer_port);
    if (fd < 0) {
        return -1;
    }

    if (pool->conn_count < HDGL_MAX_POOL_SIZE) {
        hdgl_pooled_conn_t *slot = &pool->connections[pool->conn_count++];
        memset(slot, 0, sizeof(*slot));
        slot->fd = fd;
        slot->peer_ip = peer_ip;
        slot->peer_port = peer_port;
        slot->created_at = time(NULL);
        slot->last_used = slot->created_at;
        slot->request_count = 1;
        slot->is_valid = 1;
        pool->total_new += 1;
    }

    return fd;
}

int hdgl_client_send_frame(hdgl_transport_server_t *server,
                           uint32_t peer_ip,
                           hdgl_frame_t *frame,
                           hdgl_frame_t **out_response) {
    uint16_t peer_port = server ? server->port : HDGL_CLIENT_HTTP_PORT_DEFAULT;
    int rc = hdgl_transport_send_http_frame(server, peer_ip, peer_port, frame, out_response);
    if (rc != 0) {
        if (server && server->peer_pools) {
            hdgl_connection_pool_t *pool = hdgl_transport_find_pool(server, peer_ip);
            if (pool) {
                for (uint32_t i = 0; i < pool->conn_count; i++) {
                    if (pool->connections[i].is_valid) {
                        hdgl_pool_invalidate_connection(pool, pool->connections[i].fd);
                    }
                }
            }
        }
    }
    return rc;
}

int hdgl_client_send_batch(hdgl_transport_server_t *server,
                           uint32_t peer_ip,
                           hdgl_frame_t **frames,
                           uint32_t frame_count,
                           hdgl_frame_t **out_responses) {
    if (!server || !frames || frame_count == 0) {
        return -1;
    }

    for (uint32_t i = 0; i < frame_count; i++) {
        hdgl_frame_t *response = NULL;
        if (hdgl_client_send_frame(server, peer_ip, frames[i], &response) != 0) {
            return -1;
        }

        if (out_responses) {
            out_responses[i] = response;
        } else if (response) {
            if (response->payload) {
                free(response->payload);
            }
            free(response);
        }
    }

    return 0;
}

int hdgl_timestamp_is_valid(uint64_t timestamp) {
    uint64_t now_ms = (uint64_t)time(NULL) * 1000ULL;
    uint64_t diff = (now_ms > timestamp) ? (now_ms - timestamp) : (timestamp - now_ms);
    return (diff <= (uint64_t)HDGL_REPLAY_WINDOW_SEC * 1000ULL) ? 1 : 0;
}
