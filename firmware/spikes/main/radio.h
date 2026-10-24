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
#include "awdl_dedup.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
	uint64_t rx_action;     /* AWDL action frames received (BSSID match) */
	uint64_t rx_data;       /* AWDL data frames with >= 1 MSDU delivered (no dups) */
	uint64_t rx_dup;        /* AWDL data frames dropped as 802.11 retransmissions */
	uint64_t rx_amsdu;      /* of rx_data: frames that were A-MSDU aggregates */
	uint64_t rx_msdu;       /* MSDUs (L3 packets) handed to the data handler */
	uint64_t rx_data_perr;  /* body-carrying AWDL data frames that failed to parse
	                         * (also an A-MSDU cut short after good subframes,
	                         * which then counts in rx_data/rx_msdu as well) */
	uint64_t rx_other_bssid;/* frames seen in promiscuous with a different BSSID */
	uint64_t rx_parse_err;  /* AWDL-looking frames that failed to parse */
	uint64_t tx_ok;         /* esp_wifi_80211_tx calls that returned ESP_OK */
	uint64_t tx_err;        /* esp_wifi_80211_tx calls that returned an error */
	uint64_t tx_done_ok;    /* 80211 tx-done callbacks with status SUCCESS */
	uint64_t tx_done_fail;  /* 80211 tx-done callbacks with status FAIL */
	uint64_t tx_uc_ack;     /* our unicast DATA frames the peer hardware-ACKed */
	uint64_t tx_uc_noack;   /* our unicast DATA frames with no ACK after retries */
	/* Bring-up checks for a new chip (C5): the promiscuous type disagreed with
	 * the frame-control type bits, and whether the 4 bytes after the frame are
	 * really its FCS (CRC-32 over AWDL action frames, so sig_len includes it). */
	uint64_t rx_type_mismatch;
	uint64_t rx_fcs_ok;
	uint64_t rx_fcs_bad;
	/* Where the FCS really is when it is not at sig_len - 4: right after
	 * sig_len (sig_len would then exclude it), or after dump_len (Wi-Fi MAC v3
	 * chips such as the C5 report that field). Counted for action frames. */
	uint64_t rx_fcs_at_sig;
	uint64_t rx_fcs_at_dump;
	/* Where the TLV chain of an action frame really ends (needs no FCS):
	 * at sig_len - 4 (FCS included in sig_len, as on the C3), at sig_len (no
	 * FCS in sig_len), at dump_len, or elsewhere. */
	uint64_t rx_end_sig4;
	uint64_t rx_end_sig;
	uint64_t rx_end_dump;
	uint64_t rx_end_other;
	/* IPv6 MSDUs shorter / longer than their IPv6 header says: a wrong frame
	 * length cuts real bytes off (short) or keeps trailing bytes (long). */
	uint64_t rx_ip6_short;
	uint64_t rx_ip6_long;
} radio_stats_t;

/* Snapshot of the last AWDL action frame's length fields, for bring-up. */
typedef struct {
	uint32_t count;       /* action frames seen so far */
	uint16_t sig_len;     /* rx_ctrl.sig_len */
	uint16_t dump_len;    /* rx_ctrl.dump_len (Wi-Fi MAC v3), else 0 */
	uint8_t  rx_state;    /* rx_ctrl.rx_state, 0 = no error */
	uint8_t  tail[16];    /* frame bytes [sig_len - 8, sig_len + 8) */
	uint16_t tlv_end;     /* where the TLV chain ends (awdl_action_tlv_end) */
	bool     fcs_at_sig_minus4, fcs_at_sig, fcs_at_dump;
} radio_rx_probe_t;
void radio_get_rx_probe(radio_rx_probe_t *out);

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
 * Bring up Wi-Fi in station mode without connecting, on the given channel
 * (see radio_channel_supported). Safe to call once at startup. Returns an
 * esp_err_t (0 == ESP_OK).
 */
int radio_init(uint8_t channel);

/*
 * Whether this chip can be put on the channel: 1..13 everywhere, and on a 5 GHz
 * chip (ESP32-C5) also 36..48 and 149..173 (steps of 4; 173 ends at 5875 MHz,
 * the top of the EU short-range-device band). The DFS channels 52..144 are left
 * out on purpose: transmitting there needs radar detection. This list is the
 * real limit: with the driver's default country ("01", 802.11d) the driver
 * itself accepts 1..14 and every 5 GHz channel.
 */
bool radio_channel_supported(uint8_t channel);

/* Our AWDL self address = the station interface MAC. Copies 6 bytes out. */
void radio_get_self_mac(uint8_t out[6]);

/*
 * Override the station MAC (for the ACK experiment: AWDL-addr == iface MAC vs
 * not). Stops and restarts Wi-Fi internally. bit0 of mac[0] must be 0.
 * Returns esp_err_t.
 */
int radio_set_self_mac(const uint8_t mac[6]);

/* Set the channel (see radio_channel_supported). Applies the TX power cap for
 * the target channel first when moving up into the 25 mW band (149+), so we
 * never transmit there above it. Returns esp_err_t. */
int radio_set_channel(uint8_t channel);
uint8_t radio_get_channel(void);

/*
 * Switch the Wi-Fi radio off (radio_sleep: promiscuous off, esp_wifi_stop) and
 * on again (radio_wake: esp_wifi_start, power save off, country, band, channel
 * and TX power re-applied, promiscuous back if it was on). For main/power.c;
 * the AWDL task must be paused first (awdl_app_sleep). While asleep,
 * radio_set_channel only records the channel and radio_tx fails.
 */
int radio_sleep(void);
int radio_wake(void);
bool radio_asleep(void);

/*
 * TX power ceiling in 0.25 dBm steps (8..84, e.g. 80 = 20 dBm), as for
 * esp_wifi_set_max_tx_power. The effective ceiling is the lower of this and the
 * 11 dBm cap on channels 149+ (25 mW EIRP, short-range-device band in the EU).
 * Returns esp_err_t. radio_get_max_tx_power reports what the driver applies.
 */
int radio_set_max_tx_power(int8_t qdbm);
int8_t radio_get_max_tx_power(void);

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
