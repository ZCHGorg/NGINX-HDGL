/*
 * hdgl_lattice.h - Phi-Spiral Geometry & Strand Routing
 *
 * Pure HDGL strand architecture:
 * - 8 geometric strands (A-H): Point, Line, Triangle, Tetrahedron, etc.
 * - Phi-spiral weighting (golden ratio φ = 1.618...)
 * - EMA latency smoothing
 * - Path → Strand mapping via phi-tau hash
 */

#ifndef HDGL_LATTICE_H
#define HDGL_LATTICE_H

#include "hdgl_core.h"

/* ============================================================================
 * EMA (Exponential Moving Average) Computation
 * ============================================================================ */

#define HDGL_EMA_ALPHA              0.3    /* Smoothing factor */
#define HDGL_EMA_INITIAL            50.0   /* Starting estimate (ms) */

/* Update EMA with new measurement */
double hdgl_ema_update(double current_ema, double new_value);

/* ============================================================================
 * Phi-Spiral Weight Computation
 * ============================================================================ */

/* Amplify raw weight using phi-spiral function */
uint8_t hdgl_compute_strand_weight(double latency_ema, double storage_available);

/* Phi-spiral function: w(x) = x^1.2 (phi-weighted amplification) */
double hdgl_phi_amplify(double x);

/* ============================================================================
 * Phi-Tau Hash (Path → Strand Routing)
 * ============================================================================ */

/* Compute phi-tau hash for a path (deterministic) */
uint64_t hdgl_compute_phi_tau(const char *path, size_t path_len);

/* Map phi-tau hash to strand ID (0-7) */
uint8_t hdgl_phi_tau_to_strand(uint64_t phi_tau);

/* Map phi-tau hash to authority node */
uint32_t hdgl_phi_tau_to_authority(hdgl_lattice_t *lattice, uint64_t phi_tau);

/* ============================================================================
 * Lattice Updates (Gossip Driven)
 * ============================================================================ */

/* Update lattice with gossip from peer */
int hdgl_lattice_apply_gossip(hdgl_lattice_t *lattice, uint32_t peer_ip, hdgl_gossip_msg_t *msg);

/* Compute full lattice state (provisioner pass) */
int hdgl_lattice_recompute(hdgl_lattice_t *lattice);

/* Get current authority for a strand */
uint32_t hdgl_lattice_get_strand_authority(hdgl_lattice_t *lattice, uint8_t strand_id);

/* Update self strand weights based on local metrics */
int hdgl_lattice_update_self_metrics(hdgl_lattice_t *lattice, double latency_ms, double storage_available);

/* ============================================================================
 * Cluster Fingerprint (Convergence Indicator)
 * ============================================================================ */

/* Compute 32-bit cluster fingerprint from lattice state */
uint32_t hdgl_lattice_compute_fingerprint(hdgl_lattice_t *lattice);

/* Hamming distance between fingerprints (0-32 bits) */
uint32_t hdgl_fingerprint_hamming_distance(uint32_t fp1, uint32_t fp2);

/* ============================================================================
 * PROVISIONER Pass (EMA → SCALE → PHASESHIFT → OMEGAMULT → ENERGY → FOLD256)
 * ============================================================================ */

/* NORM: normalize latencies to [0, 1] range */
void hdgl_provisioner_norm(hdgl_lattice_t *lattice);

/* SCALE: phi-spiral amplification */
void hdgl_provisioner_scale(hdgl_lattice_t *lattice);

/* PHASESHIFT: rotate strand authority based on cycle */
void hdgl_provisioner_phaseshift(hdgl_lattice_t *lattice, uint64_t cycle);

/* OMEGAMULT: fibonacci-weighted stabilization */
void hdgl_provisioner_omegamult(hdgl_lattice_t *lattice);

/* ENERGY: compute authority energy per strand */
void hdgl_provisioner_energy(hdgl_lattice_t *lattice);

/* FOLD256: final fold to 32-bit fingerprint */
void hdgl_provisioner_fold256(hdgl_lattice_t *lattice);

/* Full provisioner pipeline */
int hdgl_provisioner_run(hdgl_lattice_t *lattice, uint64_t cycle);

/* ============================================================================
 * Strand Assignment (My Strands)
 * ============================================================================ */

/* Determine which strands this node is authority for */
int hdgl_lattice_compute_my_strands(hdgl_lattice_t *lattice, uint8_t *out_strands, uint8_t *out_count);

/* ============================================================================
 * Omega-TTL Caching Model
 * ============================================================================ */

/* Compute strand-aware TTL using alpha model */
uint32_t hdgl_compute_omega_ttl(uint8_t strand_id, uint64_t cycle);

#endif /* HDGL_LATTICE_H */
