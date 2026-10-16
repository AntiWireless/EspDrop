/*
 * EspDrop - ESP32 Wi-Fi radio glue for the AWDL spikes.
 *
 * Thin layer over ESP-IDF esp_wifi: raw 802.11 transmit, promiscuous receive
 * filtered to the AWDL BSSID, and the counters/peer events the console prints.
 * This is the ONLY place that talks to ESP-IDF; awdl_frame.* stays portable.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_RADIO_H
#define ESPDROP_RADIO_H

#include <stdbool.h>
#include <stdint.h>

#include "awdl_frame.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
	uint64_t rx_action;     /* AWDL action frames received (BSSID match) */
	uint64_t rx_data;       /* AWDL data frames received */
	uint64_t rx_other_bssid;/* frames seen in promiscuous with a different BSSID */
	uint64_t rx_parse_err;  /* AWDL-looking frames that failed to parse */
	uint64_t tx_ok;         /* esp_wifi_80211_tx calls that returned ESP_OK */
	uint64_t tx_err;        /* esp_wifi_80211_tx calls that returned an error */
	uint64_t tx_done_ok;    /* 80211 tx-done callbacks with status SUCCESS */
	uint64_t tx_done_fail;  /* 80211 tx-done callbacks with status FAIL */
} radio_stats_t;

/*
 * Handler for a received, parsed AWDL action frame. Invoked from Wi-Fi task
 * context inside the promiscuous callback, so it MUST be quick and must not
 * block (copy what it needs into a queue and return). pv is only valid for the
 * duration of the call. now_us is esp_timer_get_time() at reception.
 */
typedef void (*radio_action_handler_t)(const awdl_peer_view_t *pv, int8_t rssi,
                                       uint8_t channel, uint64_t now_us, void *ctx);

/*
 * Handler for a received, parsed AWDL *data* frame, reconstructed as Ethernet:
 * dst = 802.11 addr1, src = addr2, plus the ethertype and L3 payload the AWDL
 * data header carried. Invoked from Wi-Fi task context inside the promiscuous
 * callback, so it MUST be quick and non-blocking (copy what it needs into a
 * queue and return). All pointers are only valid for the duration of the call.
 */
typedef void (*radio_data_handler_t)(const uint8_t dst[6], const uint8_t src[6],
                                     uint16_t ethertype, const uint8_t *payload,
                                     size_t plen, void *ctx);

/*
 * Bring up Wi-Fi in station mode without connecting, on the given 2.4 GHz
 * channel. Safe to call once at startup. Returns an esp_err_t (0 == ESP_OK).
 */
int radio_init(uint8_t channel);

/* Our AWDL self address = the station interface MAC. Copies 6 bytes out. */
void radio_get_self_mac(uint8_t out[6]);

/*
 * Override the station MAC (for the ACK experiment: AWDL-addr == iface MAC vs
 * not). Stops and restarts Wi-Fi internally. bit0 of mac[0] must be 0.
 * Returns esp_err_t.
 */
int radio_set_self_mac(const uint8_t mac[6]);

/* Set the 2.4 GHz channel (1..13). Returns esp_err_t. */
int radio_set_channel(uint8_t channel);
uint8_t radio_get_channel(void);

/* Inject a raw 802.11 frame (MAC header .. no FCS). Returns esp_err_t. */
int radio_tx(const uint8_t *buf, int len);

/* Enable/disable promiscuous receive with the AWDL action+data callback. */
int radio_promisc(bool on);
bool radio_promisc_enabled(void);

/* Copy current counters out. */
void radio_get_stats(radio_stats_t *out);
void radio_reset_stats(void);

/* Register the consumer for received AWDL action frames (NULL to clear). */
void radio_set_action_handler(radio_action_handler_t h, void *ctx);

/* Register the consumer for received AWDL data frames (NULL to clear). */
void radio_set_data_handler(radio_data_handler_t h, void *ctx);

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_RADIO_H */
