/*
 * EspDrop - AWDL peer table and election run loop (platform independent).
 *
 * See awdl_peers.h. Election logic ported from OWL src/election.c
 * awdl_election_run() (GPL-3.0), over a fixed array instead of OWL's hashmap.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_peers.h"

#include <string.h>

void awdl_peers_init(awdl_peer_table_t *t) {
	memset(t, 0, sizeof(*t));
	t->timeout_us = AWDL_PEER_TIMEOUT_US;
	t->clean_interval_us = AWDL_PEER_CLEAN_INTERVAL_US;
}

awdl_peer_t *awdl_peers_get(awdl_peer_table_t *t, const uint8_t addr[6]) {
	for (int i = 0; i < AWDL_MAX_PEERS; i++)
		if (t->peers[i].used && memcmp(t->peers[i].addr, addr, 6) == 0)
			return &t->peers[i];
	return NULL;
}

awdl_peer_t *awdl_peers_add(awdl_peer_table_t *t, const uint8_t addr[6],
                            uint64_t now_us, bool *created) {
	if (created) *created = false;

	awdl_peer_t *p = awdl_peers_get(t, addr);
	if (p) {
		p->last_update_us = now_us;
		return p;
	}
	for (int i = 0; i < AWDL_MAX_PEERS; i++) {
		if (!t->peers[i].used) {
			p = &t->peers[i];
			memset(p, 0, sizeof(*p));
			p->used = true;
			memcpy(p->addr, addr, 6);
			p->last_update_us = now_us;
			awdl_election_init(&p->election, addr);
			p->is_valid = true;
			if (created) *created = true;
			return p;
		}
	}
	return NULL; /* table full */
}

int awdl_peers_count(const awdl_peer_table_t *t) {
	int n = 0;
	for (int i = 0; i < AWDL_MAX_PEERS; i++)
		if (t->peers[i].used) n++;
	return n;
}

int awdl_peers_age(awdl_peer_table_t *t, uint64_t cutoff_us) {
	int removed = 0;
	for (int i = 0; i < AWDL_MAX_PEERS; i++) {
		if (t->peers[i].used && t->peers[i].last_update_us < cutoff_us) {
			t->peers[i].used = false;
			removed++;
		}
	}
	return removed;
}

static int cmp_u32(uint32_t a, uint32_t b) {
	return (a < b) ? -1 : (a > b);
}

/* Compare two election states by (master_counter, master_metric). */
static int election_cmp_master(const awdl_election_t *a, const awdl_election_t *b) {
	int r = cmp_u32(a->master_counter, b->master_counter);
	if (!r) r = cmp_u32(a->master_metric, b->master_metric);
	return r;
}

void awdl_election_run(awdl_election_t *self, const awdl_peer_table_t *t) {
	awdl_election_reset_self(self);
	const awdl_election_t *master = self;

	for (int i = 0; i < AWDL_MAX_PEERS; i++) {
		if (!t->peers[i].used) continue;
		const awdl_peer_t *peer = &t->peers[i];
		const awdl_election_t *ps = &peer->election;

		if (!peer->is_valid)
			continue;
		if (ps->height + 1 > AWDL_ELECTION_TREE_MAX_HEIGHT)
			continue; /* sync tree would get too tall */
		if (awdl_election_is_sync_master(ps, self->self_addr))
			continue; /* would create a cycle */

		int cmp = election_cmp_master(ps, master);
		if (cmp < 0) {
			continue; /* weaker top master */
		} else if (cmp == 0) {
			if (ps->height > master->height) {
				continue; /* would lengthen the sync tree */
			} else if (ps->height == master->height) {
				if (awdl_addr_cmp(ps->self_addr, master->self_addr) <= 0)
					continue; /* tie-break: peer has the smaller address */
			}
		}
		master = ps; /* accept */
	}

	if (master != self) {
		memcpy(self->master_addr, master->master_addr, 6);
		memcpy(self->sync_addr, master->self_addr, 6);
		self->master_metric = master->master_metric;
		self->master_counter = master->master_counter;
		self->height = master->height + 1;
	}
}
