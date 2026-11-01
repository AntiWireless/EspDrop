/*
 * EspDrop - AWDL peer table and master election run loop (platform independent).
 *
 * A fixed-size table (no dynamic allocation, so it is safe on the ESP32 and
 * testable on a host). Trimmed from OWL src/peers.c: we keep the fields the
 * sync/election/scheduler need and drop the hashmap and neighbour-table
 * plumbing. Election iterates this table, so awdl_election_run lives here.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_AWDL_PEERS_H
#define ESPDROP_AWDL_PEERS_H

#include <stdbool.h>
#include <stdint.h>

#include "awdl_frame.h"     /* AWDL_CHANSEQ_LEN, AWDL_NAME_MAX */
#include "awdl_election.h"

#ifdef __cplusplus
extern "C" {
#endif

#ifndef AWDL_MAX_PEERS
#define AWDL_MAX_PEERS 16
#endif

#define AWDL_PEER_TIMEOUT_US        2000000 /* evict a peer unseen this long */
#define AWDL_PEER_CLEAN_INTERVAL_US 1000000 /* how often to run the cleaner */

typedef struct {
	bool     used;
	uint8_t  addr[6];
	uint64_t last_update_us;

	bool     has_name;
	char     name[AWDL_NAME_MAX + 1];

	bool     has_version;
	uint8_t  version;
	uint8_t  devclass;

	bool     has_chanseq;
	uint8_t  chanseq_enc;
	uint8_t  chanseq[AWDL_CHANSEQ_LEN]; /* decoded channel numbers, 0 = absent */

	awdl_election_t election;
	bool     supports_v2;

	bool     has_sync_offset;
	int64_t  sync_offset_us; /* our AW phase -> this peer's; +ve = peer ahead */

	bool     is_valid;       /* eligible to be elected master */
} awdl_peer_t;

typedef struct {
	awdl_peer_t peers[AWDL_MAX_PEERS];
	uint64_t    timeout_us;
	uint64_t    clean_interval_us;
} awdl_peer_table_t;

void awdl_peers_init(awdl_peer_table_t *t);

/* Look up a peer by address, or NULL. */
awdl_peer_t *awdl_peers_get(awdl_peer_table_t *t, const uint8_t addr[6]);

/*
 * Get the peer for addr, creating it if new (returns NULL only if the table is
 * full). Sets last_update_us = now and, on creation, initialises election and
 * marks the peer valid. *created is set true if a new entry was made.
 */
awdl_peer_t *awdl_peers_add(awdl_peer_table_t *t, const uint8_t addr[6],
                            uint64_t now_us, bool *created);

int awdl_peers_count(const awdl_peer_table_t *t);

/* Remove peers whose last_update_us is older than cutoff_us. Returns count removed. */
int awdl_peers_age(awdl_peer_table_t *t, uint64_t cutoff_us);

/*
 * Run the election: pick our sync master among self and the valid peers, by
 * (master_counter, master_metric), then lower height, then larger address.
 * Updates *self in place (sync_addr/master_addr/height/...).
 */
void awdl_election_run(awdl_election_t *self, const awdl_peer_table_t *t);

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_AWDL_PEERS_H */
