/*
 * EspDrop - AWDL node brain (platform independent). See awdl_node.h.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_node.h"

#include <string.h>

void awdl_node_init(awdl_node_t *n, const uint8_t self_addr[6],
                    const char *name, uint8_t chan_num, uint64_t now_us) {
	memset(n, 0, sizeof(*n));
	awdl_self_init(&n->self, self_addr, name, chan_num);
	awdl_sync_init(&n->sync, now_us);
	awdl_election_init(&n->election, self_addr);
	awdl_peers_init(&n->peers);
	n->last_sync_err_tu = 0;
	n->have_master = false;
}

static void peer_apply_view(awdl_peer_t *p, const awdl_peer_view_t *pv) {
	if (pv->has_name) {
		memcpy(p->name, pv->name, sizeof(p->name));
		p->has_name = true;
	}
	if (pv->has_version) {
		p->version = pv->version;
		p->devclass = pv->devclass;
		p->has_version = true;
	}
	if (pv->has_chanseq) {
		p->chanseq_enc = pv->chanseq_enc;
		memcpy(p->chanseq, pv->chanseq, sizeof(p->chanseq));
		p->has_chanseq = true;
	}
	if (pv->has_election) {
		/* The peer's election "self" is the peer itself. */
		memcpy(p->election.self_addr, pv->src_addr, 6);
		memcpy(p->election.master_addr, pv->master_addr, 6);
		memcpy(p->election.sync_addr, pv->sync_addr, 6);
		p->election.height = pv->el_height;
		p->election.master_metric = pv->el_master_metric;
		p->election.master_counter = pv->el_master_counter;
		p->election.self_metric = pv->el_self_metric;
		p->election.self_counter = pv->el_self_counter;
	}
}

/* Re-derive the channel sequence we advertise. When we track a master that has
 * advertised a channel sequence naming our channel, mirror its slots on our own
 * channel. Otherwise keep the last mirror we derived; only before the very
 * first mirror (just after boot) do we advertise the lone static sequence. */
static void awdl_node_update_chanseq(awdl_node_t *n) {
	if (n->have_master) {
		awdl_peer_t *m = awdl_peers_get(&n->peers, n->election.sync_addr);
		if (m && m->has_chanseq &&
		    awdl_self_set_chanseq_intersect(&n->self, m->chanseq, n->self.master_chan)) {
			n->seq_derived = true;
			return;
		}
	}
	if (!n->seq_derived)
		awdl_self_set_chanseq_static(&n->self, n->self.master_chan);
	/* else: keep the last good mirror (see seq_derived in awdl_node.h) */
}

void awdl_node_on_rx_action(awdl_node_t *n, const awdl_peer_view_t *pv,
                            int8_t rssi, uint64_t now_us) {
	(void)rssi; /* RSSI filtering is a later refinement */

	bool created;
	awdl_peer_t *p = awdl_peers_add(&n->peers, pv->src_addr, now_us, &created);
	if (!p) return; /* table full */

	peer_apply_view(p, pv);

	/* Re-run election now so a new or changed master is adopted immediately,
	 * then (if this sender is our master) slave our clock to its timing. */
	awdl_election_run(&n->election, &n->peers);
	n->have_master = !awdl_election_is_sync_master(&n->election, n->election.self_addr);
	if (!n->have_master) n->sync_locked = false; /* no master: next adopt is fresh */

	awdl_node_update_chanseq(n);

	if (pv->has_sync) {
		int64_t err = awdl_sync_error_tu(now_us, pv->sync_tx_down_tu,
		                                 pv->sync_aw_counter, &n->sync);
		p->sync_offset_us = err * (int64_t)AWDL_TU_US;
		p->has_sync_offset = true;
		if (awdl_election_is_sync_master(&n->election, pv->src_addr)) {
			/* First lock to this master: just adopt its clock; the pre-update
			 * "error" is only the gap between two unrelated epochs, not drift,
			 * so report 0. Thereafter it is the real residual drift. */
			bool fresh = !n->sync_locked ||
			             memcmp(n->synced_master, pv->src_addr, 6) != 0;
			n->last_sync_err_tu = fresh ? 0 : err;
			awdl_sync_update(now_us, pv->sync_tx_down_tu, pv->sync_aw_counter, &n->sync);
			memcpy(n->synced_master, pv->src_addr, 6);
			n->sync_locked = true;
		}
	}
}

void awdl_node_tick(awdl_node_t *n, uint64_t now_us) {
	if (now_us > n->peers.timeout_us)
		awdl_peers_age(&n->peers, now_us - n->peers.timeout_us);
	awdl_election_run(&n->election, &n->peers);
	n->have_master = !awdl_election_is_sync_master(&n->election, n->election.self_addr);
	if (!n->have_master) n->sync_locked = false; /* no master: next adopt is fresh */
	awdl_node_update_chanseq(n);
}

int awdl_node_build_action(awdl_node_t *n, uint8_t *buf, size_t cap,
                           uint8_t subtype, uint64_t now_us) {
	uint16_t txd = awdl_sync_next_aw_tu(now_us, &n->sync);
	uint16_t awc = awdl_sync_current_aw(now_us, &n->sync);
	return awdl_build_action(buf, cap, &n->self, subtype, now_us, txd, awc);
}

bool awdl_node_tx_slot_ready(const awdl_node_t *n, uint64_t now_us) {
	uint16_t slot = awdl_sync_current_eaw(now_us, &n->sync) % AWDL_CHANSEQ_LEN;
	return n->self.seq[slot][0] != 0; /* non-zero channel => we advertise presence */
}

void awdl_node_status(const awdl_node_t *n, awdl_node_status_t *out) {
	out->have_master = n->have_master;
	memcpy(out->master_addr, n->election.sync_addr, 6);
	out->height = n->election.height;
	out->last_sync_err_tu = n->last_sync_err_tu;
	out->peer_count = awdl_peers_count(&n->peers);
	out->seq_derived = n->seq_derived;
	for (unsigned i = 0; i < AWDL_CHANSEQ_LEN; i++)
		out->self_chanseq[i] = n->self.seq[i][0];
	const awdl_peer_t *m = n->have_master
		? awdl_peers_get((awdl_peer_table_t *)&n->peers, n->election.sync_addr) : NULL;
	out->master_has_chanseq = m && m->has_chanseq;
	for (unsigned i = 0; i < AWDL_CHANSEQ_LEN; i++)
		out->master_chanseq[i] = out->master_has_chanseq ? m->chanseq[i] : 0;
}
