/*
 * EspDrop - AWDL master election (platform independent).
 *
 * Ported from OWL src/election.{c,h} (GPL-3.0). Each node advertises an
 * election state; a node adopts as its sync master whichever peer has the
 * better (master_counter, master_metric), breaking ties by lower sync-tree
 * height and then by MAC address. OWL's defaults keep our own metric low so a
 * real Apple device always wins and we slave to it.
 *
 * The run loop that iterates the peer table lives in awdl_peers.c to avoid a
 * circular dependency; this file holds the per-node state and comparisons.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_AWDL_ELECTION_H
#define ESPDROP_AWDL_ELECTION_H

#include <stdbool.h>
#include <stdint.h>

#include "awdl_frame.h" /* AWDL_ELECTION_METRIC_INIT / _COUNTER_INIT (single source) */

#ifdef __cplusplus
extern "C" {
#endif

#define AWDL_ELECTION_TREE_MAX_HEIGHT 10

typedef struct {
	uint8_t  master_addr[6];
	uint8_t  sync_addr[6];
	uint8_t  self_addr[6];
	uint32_t height;
	uint32_t master_metric;
	uint32_t self_metric;
	uint32_t master_counter;
	uint32_t self_counter;
} awdl_election_t;

/* memcmp on 6 bytes; <0, 0, >0 like memcmp. */
int awdl_addr_cmp(const uint8_t a[6], const uint8_t b[6]);

void awdl_election_init(awdl_election_t *e, const uint8_t self[6]);

/* True if addr is the node this election state currently syncs to. */
bool awdl_election_is_sync_master(const awdl_election_t *e, const uint8_t addr[6]);

/* Reset self/master fields to "we are our own master" (used before a run). */
void awdl_election_reset_self(awdl_election_t *e);

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_AWDL_ELECTION_H */
