/*
 * EspDrop - AWDL node brain (platform independent).
 *
 * Ties together our transmit state, the AW sync clock, the election state and
 * the peer table. This is the logic OWL's daemon/core.c drives from its event
 * loop, factored out with no ESP-IDF dependency so it stays host-testable; the
 * firmware (main/) only provides a task, timers and the radio.
 *
 * All timestamps are microseconds on one monotonic clock (esp_timer on the
 * ESP32). Call awdl_node_on_rx_action() for every received AWDL action frame
 * and awdl_node_tick() periodically (~1 Hz) to age peers and re-run election.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_AWDL_NODE_H
#define ESPDROP_AWDL_NODE_H

#include <stdbool.h>
#include <stdint.h>

#include "awdl_frame.h"
#include "awdl_sync.h"
#include "awdl_election.h"
#include "awdl_peers.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
	awdl_self_t       self;     /* transmit state (addr, name, channel sequence) */
	awdl_sync_t       sync;     /* our AW clock */
	awdl_election_t   election; /* our election state (who we sync to) */
	awdl_peer_table_t peers;    /* known peers */

	/* Drift (TU) measured against our master just before the latest correction.
	 * Small and stable means we are well synced. Zero on the first lock to a
	 * master (there the "error" is only the gap between two unrelated clocks,
	 * not drift), and real drift on every subsequent frame from that master. */
	int64_t last_sync_err_tu;
	bool    have_master;        /* we sync to a peer, not to ourselves */
	bool    sync_locked;        /* we have adopted the current master's timing */
	uint8_t synced_master[6];   /* the master we are currently tracking */

	/* Our advertised sequence has been mirrored from a master at least once.
	 * From then on we keep the last good mirror whenever the master briefly
	 * ages out or names our channel nowhere, instead of falling back to the flat
	 * all-one-channel sequence (which iOS 26 refuses to send unicast to). */
	bool    seq_derived;
} awdl_node_t;

/* now_us: a monotonic microsecond timestamp (esp_timer_get_time() on hardware). */
void awdl_node_init(awdl_node_t *n, const uint8_t self_addr[6],
                    const char *name, uint8_t chan_num, uint64_t now_us);

/* Fold a received, parsed action frame into the node state. */
void awdl_node_on_rx_action(awdl_node_t *n, const awdl_peer_view_t *pv,
                            int8_t rssi, uint64_t now_us);

/* Age out stale peers and re-run election. Call ~1 Hz. */
void awdl_node_tick(awdl_node_t *n, uint64_t now_us);

/* Build one of our own PSF/MIF frames with timing from our AW clock. */
int awdl_node_build_action(awdl_node_t *n, uint8_t *buf, size_t cap,
                           uint8_t subtype, uint64_t now_us);

/*
 * True if, at now_us, the EAW slot we are in is one our advertised channel
 * sequence marks as present (non-zero). Transmitting only in these slots keeps
 * our behaviour consistent with what we advertise, and (since we are physically
 * on our channel every slot) lines our frames up with the slots the master is
 * also on that channel, i.e. when it can actually hear us. When we advertise a
 * flat sequence (no master), every slot is present, so this is always true.
 */
bool awdl_node_tx_slot_ready(const awdl_node_t *n, uint64_t now_us);

typedef struct {
	bool     have_master;
	uint8_t  master_addr[6];
	uint32_t height;
	int64_t  last_sync_err_tu;
	int      peer_count;
	bool     seq_derived;                         /* see awdl_node_t */
	uint8_t  self_chanseq[AWDL_CHANSEQ_LEN];      /* what we advertise (channel numbers) */
	bool     master_has_chanseq;
	uint8_t  master_chanseq[AWDL_CHANSEQ_LEN];    /* the tracked master's, if known */
} awdl_node_status_t;

void awdl_node_status(const awdl_node_t *n, awdl_node_status_t *out);

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_AWDL_NODE_H */
