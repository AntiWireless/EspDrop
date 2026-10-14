/*
 * EspDrop - AWDL master election state (platform independent).
 *
 * See awdl_election.h. Ported from OWL src/election.c (GPL-3.0). The run loop
 * is in awdl_peers.c.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_election.h"

#include <string.h>

int awdl_addr_cmp(const uint8_t a[6], const uint8_t b[6]) {
	return memcmp(a, b, 6);
}

void awdl_election_reset_self(awdl_election_t *e) {
	e->height = 0;
	memcpy(e->master_addr, e->self_addr, 6);
	memcpy(e->sync_addr, e->self_addr, 6);
	e->master_metric = e->self_metric;
	e->master_counter = e->self_counter;
}

void awdl_election_init(awdl_election_t *e, const uint8_t self[6]) {
	memcpy(e->self_addr, self, 6);
	e->self_counter = AWDL_ELECTION_COUNTER_INIT;
	e->self_metric = AWDL_ELECTION_METRIC_INIT;
	awdl_election_reset_self(e);
}

bool awdl_election_is_sync_master(const awdl_election_t *e, const uint8_t addr[6]) {
	return awdl_addr_cmp(e->sync_addr, addr) == 0;
}
