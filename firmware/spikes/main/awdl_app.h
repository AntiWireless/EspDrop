/*
 * EspDrop - AWDL application layer (ESP32).
 *
 * Owns the platform-independent awdl_node brain and drives it from one FreeRTOS
 * task: received action frames arrive via a queue (fed by the radio handler),
 * and PSF/MIF transmission plus peer cleanup are time-driven off
 * esp_timer_get_time(). The node is only ever touched under one mutex, so the
 * console can read a consistent snapshot.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_AWDL_APP_H
#define ESPDROP_AWDL_APP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "awdl_node.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Start the AWDL task on the given self MAC, name and 2.4 GHz channel. Registers
 * the radio action handler. Call once, after radio_init(). Returns 0 on success.
 */
int awdl_app_start(const uint8_t self_mac[6], const char *name, uint8_t chan);

/* Rebuild the node for a new self MAC / channel (resets sync). For the console
 * mac/chan commands. Safe to call at runtime. */
void awdl_app_set_self(const uint8_t self_mac[6], uint8_t chan);

/* Copy a consistent status snapshot out. Returns false if not started. */
bool awdl_app_status(awdl_node_status_t *out);

/* Format the current peer list into buf (NUL-terminated). Returns bytes used. */
int awdl_app_format_peers(char *buf, size_t cap);

/* Build and transmit one of our own PSF/MIF frames immediately (console `tx`). */
void awdl_app_tx(uint8_t subtype);

/*
 * Queue one host Ethernet frame (dst||src||ethertype||payload) for transmission
 * as an AWDL data frame. Only IPv6 (ethertype 0x86dd) is forwarded; other types
 * are ignored. The frame is sent from the AWDL task during a slot we advertise
 * as present, so it lines up with when the peer is on our channel. Signature
 * matches bridge_frame_sink_t so it can be registered with bridge_set_frame_sink.
 * Returns 0 if queued (or intentionally ignored), -1 on overflow/bad input.
 */
int awdl_app_tx_ethernet(const uint8_t *eth, size_t len, void *ctx);

/*
 * Register a callback for peer-table changes: added=true when a peer is first
 * seen, false when it ages out. Fires from the AWDL task at ~1 Hz. Pass NULL to
 * clear. Registering re-syncs: every current peer is re-emitted as added on the
 * next tick, so a sink registered after start still learns the existing peers.
 */
typedef void (*awdl_peer_cb_t)(bool added, const uint8_t mac[6], void *ctx);
void awdl_app_set_peer_cb(awdl_peer_cb_t cb, void *ctx);

/* Re-emit every current peer as added on the next tick (e.g. when a new host
 * session attaches). Safe to call from any task. */
void awdl_app_resync_peers(void);

/* Data-TX path counters (host frames -> radio), for the `tx` diagnostic. */
typedef struct {
	uint32_t queued;    /* IPv6 frames accepted from the host into the TX queue */
	uint32_t q_drop;    /* dropped because the TX queue was full */
	uint32_t sent_uc;   /* unicast data frames handed to the radio */
	uint32_t sent_mc;   /* multicast/broadcast data frames handed to the radio */
	uint32_t tx_fail;   /* build or esp_wifi_80211_tx failures */
} awdl_app_tx_counters_t;
void awdl_app_get_tx_counters(awdl_app_tx_counters_t *out);

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_AWDL_APP_H */
