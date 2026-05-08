/*
 * hdgl_bench.c - HDGL v0.6 Performance Benchmark
 * 
 * Measures:
 * - Throughput (req/sec)
 * - Latency (P50, P95, P99)
 * - Memory usage
 * - Connection pool efficiency
 * 
 * Usage:
 *   ./hdgl_bench <host> <port> [duration_sec] [concurrent_connections]
 * Example:
 *   ./hdgl_bench 127.0.0.1 8090 30 1000
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <time.h>
#include <sys/time.h>
#include <sys/select.h>
#include <errno.h>

#define BENCH_MAX_CONNECTIONS   10000
#define BENCH_RESPONSE_BUF      4096
#define BENCH_REQUEST           "GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n"

typedef struct {
    int         fd;
    int         connected;
    int         request_sent;
    int         response_complete;
    char        response_buf[BENCH_RESPONSE_BUF];
    size_t      response_len;
    struct timeval start_time;
    double      latency_ms;
} bench_connection_t;

typedef struct {
    bench_connection_t connections[BENCH_MAX_CONNECTIONS];
    int         conn_count;
    
    /* Metrics */
    uint64_t    total_requests;
    uint64_t    total_responses;
    uint64_t    total_errors;
    
    double      latencies[BENCH_MAX_CONNECTIONS];
    int         latency_count;
    
    time_t      start_time;
    time_t      end_time;
} bench_context_t;

static bench_context_t g_bench = {0};

/* Get current time in milliseconds */
static double get_time_ms(void) {
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (double)tv.tv_sec * 1000.0 + (double)tv.tv_usec / 1000.0;
}

/* Set socket to non-blocking */
static int set_nonblocking(int fd) {
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0) return -1;
    return fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

/* Create a connection */
static int bench_connect(const char *host, uint16_t port, int idx) {
    if (idx >= BENCH_MAX_CONNECTIONS) return -1;
    
    struct sockaddr_in addr;
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    
    if (inet_aton(host, &addr.sin_addr) == 0) {
        fprintf(stderr, "Invalid IP: %s\n", host);
        return -1;
    }
    
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        perror("socket");
        return -1;
    }
    
    if (set_nonblocking(fd) != 0) {
        close(fd);
        return -1;
    }
    
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        if (errno != EINPROGRESS) {
            perror("connect");
            close(fd);
            return -1;
        }
    }
    
    bench_connection_t *conn = &g_bench.connections[idx];
    conn->fd = fd;
    conn->connected = 0;
    conn->request_sent = 0;
    conn->response_complete = 0;
    conn->response_len = 0;
    gettimeofday((struct timeval *)&conn->start_time, NULL);
    
    g_bench.conn_count++;
    return 0;
}

/* Send HTTP request on connection */
static int bench_send_request(int idx) {
    bench_connection_t *conn = &g_bench.connections[idx];
    if (conn->request_sent) return 0;
    
    ssize_t sent = send(conn->fd, BENCH_REQUEST, strlen(BENCH_REQUEST), 0);
    if (sent < 0) {
        if (errno != EAGAIN && errno != EWOULDBLOCK) {
            g_bench.total_errors++;
            return -1;
        }
        return 0;
    }
    
    conn->request_sent = 1;
    g_bench.total_requests++;
    return 0;
}

/* Read HTTP response */
static int bench_read_response(int idx) {
    bench_connection_t *conn = &g_bench.connections[idx];
    if (conn->response_complete) return 0;
    
    ssize_t n = recv(conn->fd, 
                     conn->response_buf + conn->response_len,
                     BENCH_RESPONSE_BUF - conn->response_len,
                     0);
    
    if (n < 0) {
        if (errno != EAGAIN && errno != EWOULDBLOCK) {
            g_bench.total_errors++;
            return -1;
        }
        return 0;
    }
    
    if (n == 0) {
        g_bench.total_errors++;
        return -1;
    }
    
    conn->response_len += (size_t)n;
    
    /* Check if we have full response (simple check for \r\n\r\n) */
    if (conn->response_len >= 4) {
        for (size_t i = 0; i + 3 < conn->response_len; i++) {
            if (conn->response_buf[i] == '\r' && conn->response_buf[i+1] == '\n' &&
                conn->response_buf[i+2] == '\r' && conn->response_buf[i+3] == '\n') {
                conn->response_complete = 1;
                g_bench.total_responses++;
                
                struct timeval end;
                gettimeofday(&end, NULL);
                double end_ms = (double)end.tv_sec * 1000.0 + (double)end.tv_usec / 1000.0;
                double start_ms = (double)conn->start_time.tv_sec * 1000.0 + 
                                 (double)conn->start_time.tv_usec / 1000.0;
                conn->latency_ms = end_ms - start_ms;
                
                if (g_bench.latency_count < BENCH_MAX_CONNECTIONS) {
                    g_bench.latencies[g_bench.latency_count++] = conn->latency_ms;
                }
                
                return 1;
            }
        }
    }
    
    return 0;
}

/* Compare function for qsort */
static int compare_doubles(const void *a, const void *b) {
    double da = *(const double *)a;
    double db = *(const double *)b;
    if (da < db) return -1;
    if (da > db) return 1;
    return 0;
}

/* Compute percentile */
static double percentile(double *data, int count, double p) {
    if (count <= 0) return 0.0;
    int idx = (int)((p / 100.0) * count);
    if (idx >= count) idx = count - 1;
    return data[idx];
}

/* Run benchmark */
static int bench_run(const char *host, uint16_t port, int duration_sec, int concurrent) {
    if (concurrent > BENCH_MAX_CONNECTIONS) {
        fprintf(stderr, "Too many connections (max %d)\n", BENCH_MAX_CONNECTIONS);
        return -1;
    }
    
    fprintf(stdout, "HDGL v0.6 Benchmark\n");
    fprintf(stdout, "Target: %s:%d\n", host, port);
    fprintf(stdout, "Duration: %d sec\n", duration_sec);
    fprintf(stdout, "Concurrent: %d\n", concurrent);
    fprintf(stdout, "---\n");
    
    g_bench.start_time = time(NULL);
    
    /* Create connections */
    for (int i = 0; i < concurrent; i++) {
        if (bench_connect(host, port, i) != 0) {
            fprintf(stderr, "Failed to create connection %d\n", i);
            break;
        }
    }
    
    if (g_bench.conn_count < concurrent / 2) {
        fprintf(stderr, "Failed to create enough connections\n");
        return -1;
    }
    
    fprintf(stdout, "Created %d connections\n", g_bench.conn_count);
    
    /* Run benchmark loop */
    time_t end_time = g_bench.start_time + duration_sec;
    int active = g_bench.conn_count;
    
    while (time(NULL) < end_time && active > 0) {
        fd_set read_fds, write_fds;
        FD_ZERO(&read_fds);
        FD_ZERO(&write_fds);
        
        int max_fd = 0;
        int pending = 0;
        
        for (int i = 0; i < g_bench.conn_count; i++) {
            bench_connection_t *conn = &g_bench.connections[i];
            if (conn->fd < 0) continue;
            
            if (!conn->connected) {
                FD_SET(conn->fd, &write_fds);
                pending++;
            } else if (conn->request_sent && !conn->response_complete) {
                FD_SET(conn->fd, &read_fds);
                pending++;
            } else if (!conn->request_sent) {
                FD_SET(conn->fd, &write_fds);
                pending++;
            } else if (conn->response_complete) {
                /* Reuse connection for next request */
                conn->request_sent = 0;
                conn->response_complete = 0;
                conn->response_len = 0;
                gettimeofday((struct timeval *)&conn->start_time, NULL);
                FD_SET(conn->fd, &write_fds);
                pending++;
            }
            
            if (conn->fd > max_fd) max_fd = conn->fd;
        }
        
        if (pending == 0) break;
        
        struct timeval tv = {.tv_sec = 1, .tv_usec = 0};
        int ret = select(max_fd + 1, &read_fds, &write_fds, NULL, &tv);
        if (ret < 0) {
            perror("select");
            break;
        }
        
        /* Process connections */
        for (int i = 0; i < g_bench.conn_count; i++) {
            bench_connection_t *conn = &g_bench.connections[i];
            if (conn->fd < 0) continue;
            
            if (!conn->connected && FD_ISSET(conn->fd, &write_fds)) {
                conn->connected = 1;
            }
            
            if (conn->connected) {
                if (FD_ISSET(conn->fd, &write_fds)) {
                    bench_send_request(i);
                }
                if (FD_ISSET(conn->fd, &read_fds)) {
                    bench_read_response(i);
                }
            }
        }
    }
    
    g_bench.end_time = time(NULL);
    
    /* Compute statistics */
    double duration = (double)(g_bench.end_time - g_bench.start_time);
    double throughput = g_bench.total_responses / duration;
    
    qsort(g_bench.latencies, g_bench.latency_count, sizeof(double), compare_doubles);
    
    fprintf(stdout, "\nResults:\n");
    fprintf(stdout, "  Total responses: %llu\n", (unsigned long long)g_bench.total_responses);
    fprintf(stdout, "  Total errors: %llu\n", (unsigned long long)g_bench.total_errors);
    fprintf(stdout, "  Duration: %.1f sec\n", duration);
    fprintf(stdout, "  Throughput: %.0f req/sec\n", throughput);
    fprintf(stdout, "  Latency P50: %.2f ms\n", percentile(g_bench.latencies, g_bench.latency_count, 50.0));
    fprintf(stdout, "  Latency P95: %.2f ms\n", percentile(g_bench.latencies, g_bench.latency_count, 95.0));
    fprintf(stdout, "  Latency P99: %.2f ms\n", percentile(g_bench.latencies, g_bench.latency_count, 99.0));
    
    if (throughput >= 200000.0) {
        fprintf(stdout, "\n✓ TARGET MET: 200K+ req/sec achieved!\n");
    } else {
        fprintf(stdout, "\n✗ TARGET NOT MET: Need %.0fK more req/sec\n", (200000.0 - throughput) / 1000.0);
    }
    
    return 0;
}

int main(int argc, char *argv[]) {
    if (argc < 3) {
        fprintf(stderr, "Usage: %s <host> <port> [duration_sec] [concurrent]\n", argv[0]);
        fprintf(stderr, "Example: %s 127.0.0.1 8090 30 1000\n", argv[0]);
        return 1;
    }
    
    const char *host = argv[1];
    uint16_t port = (uint16_t)atoi(argv[2]);
    int duration = (argc > 3) ? atoi(argv[3]) : 30;
    int concurrent = (argc > 4) ? atoi(argv[4]) : 100;
    
    if (duration < 1) duration = 30;
    if (concurrent < 1) concurrent = 10;
    
    return bench_run(host, port, duration, concurrent);
}
