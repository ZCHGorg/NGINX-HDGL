/*
 * hdgl_core.h - HDGL v0.6 Core Structures & Constants
 *
 * Pure C implementation of Hypergeometric Distributed Geometry Layer
 * End-to-end HDGL: strand routing, phi-spiral geometry, async I/O transport
 *
 * Performance targets:
 * - Throughput: 200K+ req/sec
 * - Latency P99: <1ms
 * - Memory: <10MB per 10K concurrent
 * - Concurrency: 500K+ connections
 */

#ifndef HDGL_CORE_H
#define HDGL_CORE_H

#include <stdint.h>
#include <stddef.h>
#include <time.h>
#include <sys/types.h>

/* ============================================================================
 * Frame Protocol (Binary, 52 bytes minimum)
 * ============================================================================ */

#define HDGL_FRAME_VERSION          1
#define HDGL_FRAME_HEADER_SIZE      52      /* Fixed header size in bytes */
#define HDGL_FRAME_MAX_PAYLOAD      (1024 * 1024)  /* 1MB max payload */

/* Frame types */
typedef enum {
    HDGL_FRAME_INFO       = 0x01,
    HDGL_FRAME_GOSSIP     = 0x02,
    HDGL_FRAME_FETCH      = 0x03,
    HDGL_FRAME_HEALTH     = 0x04,
    HDGL_FRAME_FILESWAP   = 0x05,
    HDGL_FRAME_ACK        = 0x06,
    HDGL_FRAME_ERROR      = 0x07
} hdgl_frame_type_t;

/* Frame header (binary packed, no padding) */
typedef struct {
    uint8_t     version;           /* Frame format version */
    uint8_t     type;              /* Frame type (hdgl_frame_type_t) */
    uint32_t    strand_id;         /* Target strand (0-7 = A-H) */
    uint32_t    reserved;          /* Reserved for future use */
    uint32_t    authority_ep;      /* Authority endpoint hash */
    uint32_t    source_ip;         /* Source IP (network byte order) */
    uint32_t    payload_len;       /* Payload length in bytes */
    uint64_t    timestamp;         /* Milliseconds since epoch */
    uint8_t     hmac[32];          /* HMAC-SHA256 (20 bytes reserved in header) */
} __attribute__((packed)) hdgl_frame_header_t;

/* Frame (header + payload) */
typedef struct {
    hdgl_frame_header_t header;
    uint8_t            *payload;
    size_t              payload_len;
    time_t              created_at;
} hdgl_frame_t;

/* ============================================================================
 * Strand Geometry (Phi-Spiral, 8 Strands)
 * ============================================================================ */

#define HDGL_STRAND_COUNT           8
#define HDGL_STRAND_A               0
#define HDGL_STRAND_B               1
#define HDGL_STRAND_C               2
#define HDGL_STRAND_D               3
#define HDGL_STRAND_E               4
#define HDGL_STRAND_F               5
#define HDGL_STRAND_G               6
#define HDGL_STRAND_H               7

/* Strand names (Point, Line, Triangle, Tetrahedron, Pentachoron, etc.) */
static const char *HDGL_STRAND_NAMES[] = {
    "Point", "Line", "Triangle", "Tetrahedron",
    "Pentachoron", "Hexacross", "Heptacube", "Octacube"
};

/* Phi-spiral parameters */
#define HDGL_PHI                    1.618033988749894848204586834365638
#define HDGL_SPIRAL_PERIOD          8       /* One rotation = 8 strands */

/* Strand weight (EMA-based) */
typedef struct {
    uint32_t    strand_id;
    double      latency_ema;        /* Exponential moving average latency (ms) */
    double      storage_available;  /* Available storage (bytes) */
    uint32_t    authority_weight;   /* Phi-weighted authority (1-100) */
    time_t      last_update;
} hdgl_strand_weight_t;

/* ============================================================================
 * Phi-Tau Routing (Path → Strand Mapping)
 * ============================================================================ */

/* Phi-tau hash: deterministic path → strand routing */
typedef struct {
    char       *path;               /* Logical file/request path */
    uint64_t    hash_value;         /* PHI_TAU hash */
    uint8_t     strand_id;          /* Determined strand (0-7) */
    uint32_t    authority_node;     /* Best authority for this strand */
} hdgl_phi_tau_route_t;

/* Cache entry for O(1) phi_tau lookups */
typedef struct {
    char       *path;
    uint8_t     strand_id;
    uint32_t    authority_node;
    time_t      cached_at;
    uint32_t    hit_count;          /* For cache statistics */
} hdgl_routing_cache_entry_t;

/* ============================================================================
 * Cluster State (Lattice)
 * ============================================================================ */

#define HDGL_CLUSTER_FINGERPRINT_SIZE   4   /* 32-bit fingerprint */
#define HDGL_MAX_PEERS                  256

/* Per-peer state */
typedef struct {
    uint32_t    ip_addr;            /* Peer IP (network byte order) */
    uint16_t    port;               /* Peer port */
    hdgl_strand_weight_t strands[HDGL_STRAND_COUNT];
    uint32_t    cluster_fingerprint;
    time_t      last_gossip_in;
    time_t      last_gossip_out;
    uint32_t    failed_checks;
    int         is_healthy;
} hdgl_peer_t;

/* Local lattice state */
typedef struct {
    uint32_t    local_ip;           /* This node's IP */
    uint16_t    port;               /* This node's port (8090) */
    hdgl_peer_t peers[HDGL_MAX_PEERS];
    uint32_t    peer_count;
    hdgl_strand_weight_t my_strands[HDGL_STRAND_COUNT];
    uint32_t    cluster_fingerprint;
    uint64_t    cycle_number;
    time_t      last_cycle;
} hdgl_lattice_t;

/* ============================================================================
 * Connection Pool (Per-Peer)
 * ============================================================================ */

#define HDGL_MAX_POOL_SIZE          32
#define HDGL_KEEP_ALIVE_TTL         60.0    /* Seconds */
#define HDGL_POOL_REUSE_LIMIT       64      /* Requests per connection */

/* Pooled connection state */
typedef struct {
    int         fd;                 /* Socket file descriptor */
    uint32_t    peer_ip;
    uint16_t    peer_port;
    time_t      created_at;
    time_t      last_used;
    uint32_t    request_count;      /* Requests on this connection */
    uint32_t    error_count;        /* Consecutive errors */
    int         is_valid;           /* Connection is usable */
} hdgl_pooled_conn_t;

/* Per-peer connection pool */
typedef struct {
    uint32_t    peer_ip;
    hdgl_pooled_conn_t connections[HDGL_MAX_POOL_SIZE];
    uint32_t    conn_count;
    uint32_t    total_reused;
    uint32_t    total_new;
    time_t      created_at;
} hdgl_connection_pool_t;

/* ============================================================================
 * Frame Pool (Object Reuse)
 * ============================================================================ */

#define HDGL_FRAME_POOL_SIZE        1024

typedef struct {
    hdgl_frame_t    frames[HDGL_FRAME_POOL_SIZE];
    uint8_t         in_use[HDGL_FRAME_POOL_SIZE];
    uint32_t        reused_count;
    uint32_t        allocated_count;
} hdgl_frame_pool_t;

/* ============================================================================
 * Transport Server State
 * ============================================================================ */

typedef struct {
    int         listen_fd;          /* Server listening socket */
    uint32_t    local_ip;
    uint16_t    port;
    char       *cluster_secret;     /* For HMAC signing */
    size_t      secret_len;

    /* Connection management */
    hdgl_connection_pool_t *peer_pools;
    uint32_t    pool_count;

    /* Frame management */
    hdgl_frame_pool_t frame_pool;

    /* Routing cache */
    hdgl_routing_cache_entry_t *route_cache;
    uint32_t    cache_size;
    uint32_t    cache_hits;
    uint32_t    cache_misses;

    /* Cluster state */
    hdgl_lattice_t lattice;

    /* Metrics */
    uint64_t    total_frames_sent;
    uint64_t    total_frames_recv;
    uint64_t    total_bytes_sent;
    uint64_t    total_bytes_recv;
    uint64_t    active_connections;
    uint64_t    total_connections;
    time_t      started_at;
} hdgl_transport_server_t;

/* ============================================================================
 * Gossip Protocol (Binary Encoded)
 * ============================================================================ */

#define HDGL_GOSSIP_PORT            8090
#define HDGL_GOSSIP_INTERVAL        30      /* Seconds between gossip cycles */

/* Gossip message (packed binary, ~16 bytes) */
typedef struct {
    uint32_t    source_ip;
    uint8_t     strand_weights[8];          /* Phi-weighted authority per strand */
    uint32_t    storage_available;
    uint32_t    cluster_fingerprint;
} __attribute__((packed)) hdgl_gossip_msg_t;

/* ============================================================================
 * Fileswap (Strand-Addressed Distributed FS)
 * ============================================================================ */

#define HDGL_FILESWAP_ROOT          "/opt/hdgl_swap"
#define HDGL_FILESWAP_MAX_SIZE_GB   7       /* Max fileswap size */

/* File route (path → strand → authority node) */
typedef struct {
    char       *logical_path;
    uint8_t     strand_id;
    uint32_t    authority_node_ip;
    char       *physical_path;      /* Local cache path */
    time_t      cached_at;
    uint64_t    file_size;
} hdgl_file_route_t;

/* ============================================================================
 * Configuration (from environment / site_config.json)
 * ============================================================================ */

typedef struct {
    char       *local_node_ip;
    char       *peer_ips[HDGL_MAX_PEERS];
    uint32_t    peer_count;
    char       *cluster_secret;
    char       *primary_domain;
    char       *fileswap_root;
    int         dry_run;
    int         simulation_mode;
} hdgl_config_t;

/* ============================================================================
 * Function Declarations
 * ============================================================================ */

/* Core initialization */
hdgl_transport_server_t* hdgl_server_create(hdgl_config_t *cfg);
void hdgl_server_destroy(hdgl_transport_server_t *server);

/* Phi-tau routing */
uint8_t hdgl_compute_strand_id(const char *path);
uint32_t hdgl_compute_phi_tau_hash(const char *path);

/* Frame operations */
hdgl_frame_t* hdgl_frame_alloc(hdgl_frame_pool_t *pool);
void hdgl_frame_free(hdgl_frame_pool_t *pool, hdgl_frame_t *frame);
int hdgl_frame_serialize(hdgl_frame_t *frame, uint8_t **out_buf, size_t *out_len);
int hdgl_frame_deserialize(uint8_t *buf, size_t len, hdgl_frame_t *out_frame);

/* Connection pooling */
int hdgl_pool_get_connection(hdgl_connection_pool_t *pool, int *out_fd);
void hdgl_pool_return_connection(hdgl_connection_pool_t *pool, int fd);
void hdgl_pool_invalidate_connection(hdgl_connection_pool_t *pool, int fd);

/* Lattice / cluster state */
void hdgl_lattice_update_strand_weight(hdgl_lattice_t *lattice, uint8_t strand_id, double latency_ms);
void hdgl_lattice_compute_fingerprint(hdgl_lattice_t *lattice);
uint32_t hdgl_lattice_get_authority(hdgl_lattice_t *lattice, uint8_t strand_id);

/* HMAC / security */
int hdgl_hmac_sign_frame(hdgl_frame_t *frame, const char *secret, size_t secret_len);
int hdgl_hmac_verify_frame(hdgl_frame_t *frame, const char *secret, size_t secret_len);

#endif /* HDGL_CORE_H */
