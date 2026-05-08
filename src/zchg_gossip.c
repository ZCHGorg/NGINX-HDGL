/*
 * zchg_gossip.c — Binary gossip protocol for cluster convergence
 * 
 * Implements lightweight peer-to-peer cluster updates via:
 * - Deterministic peer selection (phi-spiral ordering)
 * - Compact binary message format (~16 bytes)
 * - EMA-based cluster health tracking
 * - Cycle-aware gossip dispersion
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <arpa/inet.h>

#include "../include/zchg_core.h"
#include "../include/zchg_lattice.h"
#include "../include/zchg_transport.h"

/*
 * Select N random peers for gossip broadcast using phi-spiral ordering.
 * Deterministic but pseudo-random via cycle-based seeding.
 */
static int zchg_gossip_select_peers(const zchg_lattice_t *lattice,
                                     uint32_t *out_peers,
                                     int max_peers) {
    if (!lattice || lattice->peer_count == 0) {
        return 0;
    }

    int peer_idx = 0;
    uint64_t seed = lattice->cycle_number ^ (lattice->cluster_fingerprint << 32);
    
    /* Select up to 3 peers using phi-spiral order */
    int peers_to_select = (lattice->peer_count < 3) ? lattice->peer_count : 3;
    for (int i = 0; i < peers_to_select && peer_idx < max_peers; i++) {
        uint32_t offset = (uint32_t)((seed * zchg_PHI_NUMERATOR / zchg_PHI_DENOMINATOR) + i) % lattice->peer_count;
        if (lattice->peers[offset] != 0) {
            out_peers[peer_idx++] = lattice->peers[offset];
        }
    }

    return peer_idx;
}

/*
 * Generate a gossip message from current lattice state.
 * Compacts cycle number, fingerprint, and peer count into ~16 bytes.
 */
void zchg_gossip_create_message(const zchg_lattice_t *lattice,
                                 zchg_gossip_msg_t *out_msg) {
    memset(out_msg, 0, sizeof(*out_msg));
    
    out_msg->source_ip = lattice->my_ip;
    out_msg->source_strand = lattice->my_strand;
    out_msg->cycle_number = (uint32_t)(lattice->cycle_number & 0xFFFFFFFFULL);
    out_msg->cluster_fingerprint = lattice->cluster_fingerprint;
    out_msg->peer_count = lattice->peer_count;
    out_msg->timestamp = (uint32_t)time(NULL);
    
    /* Mark message as fresh */
    out_msg->flags = 0x01;
}

/*
 * Apply gossip-driven lattice update: merge peer state with local view.
 */
int zchg_lattice_apply_gossip(zchg_lattice_t *lattice,
                               uint32_t peer_ip,
                               zchg_gossip_msg_t *msg) {
    if (!lattice || !msg) {
        return -1;
    }

    time_t now = time(NULL);
    
    /* Reject stale messages (>60 seconds old) */
    time_t msg_age = now - (time_t)msg->timestamp;
    if (msg_age < -5 || msg_age > 60) {
        return -1;
    }

    /* Update peer's last contact time */
    if (peer_ip != 0) {
        for (int i = 0; i < lattice->peer_count; i++) {
            if (lattice->peers[i] == peer_ip) {
                lattice->peers_last_seen[i] = now;
                break;
            }
        }
    }

    /* If peer has a higher cycle number, incorporate its fingerprint */
    if (msg->cycle_number > (uint32_t)(lattice->cycle_number & 0xFFFFFFFFULL)) {
        lattice->cycle_number = msg->cycle_number;
        
        /* Blend fingerprints via EMA to smooth transitions */
        uint32_t new_fp = msg->cluster_fingerprint;
        lattice->cluster_fingerprint = (uint32_t)(
            (lattice->cluster_fingerprint * 3 + new_fp) / 4
        );
    }

    /* Update peer count if remote cluster appears larger */
    if (msg->peer_count > lattice->peer_count) {
        lattice->peer_count = msg->peer_count;
    }

    lattice->last_gossip_in = now;
    return 0;
}

/*
 * Broadcast gossip message to selected peers via transport layer.
 * Non-blocking; failures are tolerated and retried on next cycle.
 */
int zchg_gossip_broadcast(zchg_transport_server_t *server,
                          const zchg_gossip_msg_t *msg) {
    if (!server || !msg) {
        return -1;
    }

    uint32_t peers[3];
    int peer_count = zchg_gossip_select_peers(&server->lattice, peers, 3);
    
    if (peer_count <= 0) {
        return 0;  /* No peers to gossip to; not an error */
    }

    /* Send gossip frame to each selected peer (non-blocking) */
    for (int i = 0; i < peer_count; i++) {
        uint32_t peer_ip = peers[i];
        if (peer_ip == 0 || peer_ip == server->local_ip) {
            continue;
        }

        /* Create a gossip frame and send via peer transport */
        zchg_frame_t frame;
        memset(&frame, 0, sizeof(frame));
        frame.frame_type = zchg_FRAME_GOSSIP;
        frame.source_ip = server->local_ip;
        frame.source_strand = server->lattice.my_strand;
        frame.payload_len = sizeof(*msg);
        frame.payload = (uint8_t *)malloc(sizeof(*msg));
        
        if (frame.payload) {
            memcpy(frame.payload, msg, sizeof(*msg));
            
            /* Send to peer; transport layer handles connection pooling */
            zchg_client_send_frame(server, peer_ip, &frame);
            
            free(frame.payload);
            server->lattice.last_gossip_out = time(NULL);
        }
    }

    return 0;
}

/*
 * Periodic gossip cycle: generate and broadcast state to cluster.
 * Called by main event loop at zchg_GOSSIP_INTERVAL seconds.
 */
int zchg_gossip_cycle(zchg_transport_server_t *server) {
    if (!server) {
        return -1;
    }

    zchg_gossip_msg_t msg;
    zchg_gossip_create_message(&server->lattice, &msg);
    
    return zchg_gossip_broadcast(server, &msg);
}

/*
 * Health check: detect and evict unresponsive peers.
 * Runs after gossip cycle.
 */
int zchg_gossip_evict_dead_peers(zchg_lattice_t *lattice) {
    if (!lattice) {
        return -1;
    }

    time_t now = time(NULL);
    int removed = 0;

    for (int i = 0; i < lattice->peer_count; i++) {
        time_t last_seen = lattice->peers_last_seen[i];
        if (last_seen > 0 && (now - last_seen) > 120) {
            /* Peer unseen for >2 minutes; remove from roster */
            if (i < lattice->peer_count - 1) {
                memmove(&lattice->peers[i], &lattice->peers[i + 1],
                        (lattice->peer_count - i - 1) * sizeof(uint32_t));
                memmove(&lattice->peers_last_seen[i], &lattice->peers_last_seen[i + 1],
                        (lattice->peer_count - i - 1) * sizeof(time_t));
            }
            lattice->peer_count--;
            removed++;
            i--;
        }
    }

    return removed;
}
