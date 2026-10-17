/*
 * EspDrop - 802.11 duplicate (retransmission) detection. See awdl_dedup.h.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_dedup.h"
#include "awdl_wire.h"

#include <string.h>

#define FC_RETRY 0x0800u   /* frame-control flag bit 11 (byte 1, bit 3) */

void awdl_dedup_init(awdl_dedup_t *d) {
	if (d) memset(d, 0, sizeof(*d));
}

bool awdl_dedup_is_dup(awdl_dedup_t *d, const uint8_t *buf, size_t len) {
	if (!d || !buf || len < 24) return false; /* no sequence-control field */

	uint16_t fc = 0, seq = 0;
	wire_get_le16(buf, len, 0, &fc);
	wire_get_le16(buf, len, 22, &seq);
	const uint8_t *ta = buf + 10;

	/* QoS data (type 2, subtype bit 3) keeps a sequence space per TID, carried in
	 * the low 4 bits of the QoS control field right after the 24-byte header. */
	uint8_t ftype = (uint8_t)((fc >> 2) & 0x3);
	uint8_t fsub  = (uint8_t)((fc >> 4) & 0xf);
	uint8_t tid = AWDL_DEDUP_TID_NONE;
	if (ftype == 2 && (fsub & 0x8)) {
		uint8_t qos = 0;
		if (!wire_get_u8(buf, len, 24, &qos)) return false; /* truncated QoS hdr */
		tid = qos & 0x0f;
	}

	for (unsigned i = 0; i < AWDL_DEDUP_LEN; i++) {
		awdl_dedup_entry_t *e = &d->e[i];
		if (!e->used || e->tid != tid || memcmp(e->ta, ta, 6) != 0) continue;
		if ((fc & FC_RETRY) && e->seq_ctrl == seq)
			return true;          /* retransmission of a frame we already have */
		e->seq_ctrl = seq;        /* new frame from this sender: remember it */
		return false;
	}

	/* First frame from this (transmitter, TID): take a free or the oldest slot. */
	awdl_dedup_entry_t *e = &d->e[d->next];
	d->next = (uint8_t)((d->next + 1) % AWDL_DEDUP_LEN);
	e->used = true;
	memcpy(e->ta, ta, 6);
	e->tid = tid;
	e->seq_ctrl = seq;
	return false;
}
