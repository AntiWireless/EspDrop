/*
 * EspDrop - 802.11 duplicate (retransmission) detection (platform independent).
 *
 * A sender retransmits a unicast frame it did not get an ACK for, with the
 * Retry bit set and the same sequence-control value. A normal 802.11 receiver
 * discards those repeats (IEEE 802.11 duplicate detection: a frame with Retry=1
 * whose sequence control matches the last one accepted from the same
 * transmitter - and, for QoS data, the same TID - is a duplicate). A promiscuous
 * capture sees every retry, so without this each retry would reach the host as
 * a separate packet (e.g. ping reporting "+N duplicates").
 *
 * Fixed-size, round-robin table; no allocation. Not thread-safe: use from one
 * context (the Wi-Fi RX callback on the ESP32).
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_AWDL_DEDUP_H
#define ESPDROP_AWDL_DEDUP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#ifndef AWDL_DEDUP_LEN
#define AWDL_DEDUP_LEN 8   /* distinct (transmitter, TID) pairs tracked */
#endif

#define AWDL_DEDUP_TID_NONE 0xffu /* non-QoS frames share one sequence space */

typedef struct {
	bool     used;
	uint8_t  ta[6];      /* transmitter address (addr2) */
	uint8_t  tid;        /* QoS TID, or AWDL_DEDUP_TID_NONE */
	uint16_t seq_ctrl;   /* last accepted sequence control (seq << 4 | frag) */
} awdl_dedup_entry_t;

typedef struct {
	awdl_dedup_entry_t e[AWDL_DEDUP_LEN];
	uint8_t            next; /* round-robin slot to replace when full */
} awdl_dedup_t;

void awdl_dedup_init(awdl_dedup_t *d);

/*
 * buf/len: an 802.11 data frame from the MAC header (FCS stripped). Returns true
 * if it is a retransmission of the last frame accepted from that transmitter and
 * should be dropped. Otherwise records it as the latest frame and returns false.
 * Frames too short to carry a sequence-control field are never duplicates.
 */
bool awdl_dedup_is_dup(awdl_dedup_t *d, const uint8_t *buf, size_t len);

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_AWDL_DEDUP_H */
