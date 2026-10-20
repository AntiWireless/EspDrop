/*
 * EspDrop - ESP32 Wi-Fi radio glue for the AWDL spikes.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "radio.h"

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_private/wifi.h" /* esp_wifi_set_tx_done_cb: stable across IDF 5.1..6.x */
#include "esp_rom_crc.h"
#include "nvs_flash.h"
#include "sdkconfig.h"
#include "soc/soc_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

static const char *TAG = "radio";

static radio_stats_t          s_stats;
static portMUX_TYPE           s_stats_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile bool          s_promisc;
static uint8_t                s_channel = 6;
static radio_action_handler_t s_action_handler;
static void                  *s_action_ctx;
static radio_data_handler_t   s_data_handler;
static void                  *s_data_ctx;
static awdl_dedup_t           s_dedup;   /* touched only by rx_cb (Wi-Fi task) */

/* TX power ceiling (0.25 dBm units): what the user asked for, and the cap for
 * channels 149+ (5725-5875 MHz is a 25 mW EIRP short-range-device band in the
 * EU; 11 dBm conducted leaves room for a few dBi of antenna gain). */
#define TXPWR_SRD_CAP_QDBM 44
static int8_t                 s_txpwr_user = CONFIG_ESP_PHY_MAX_WIFI_TX_POWER * 4;

static inline void stat_inc(uint64_t *field) {
	portENTER_CRITICAL(&s_stats_mux);
	(*field)++;
	portEXIT_CRITICAL(&s_stats_mux);
}

/* ---- tx-done callback: records the driver's reported send status ----
 * txStatus is true when the frame left the MAC successfully. For a unicast
 * frame to a real peer that is the signal the ACK experiment cares about
 * (did the peer acknowledge); for broadcast it is always success. Uses the
 * internal esp_wifi_set_tx_done_cb API, which is present and identical on
 * ESP-IDF 5.1 through 6.x (the public esp_wifi_register_80211_tx_cb only
 * appeared in 5.5). */
static void tx_done_cb(uint8_t ifidx, uint8_t *data, uint16_t *data_len, bool txStatus) {
	(void)ifidx;
	/* Broadcast PSF/MIF always report success, so the totals above say little
	 * about whether a peer received our data. Separately count our own unicast
	 * data frames (frame control 0x08, addr1 without the group bit), whose status
	 * is the peer's hardware ACK. */
	bool uc_data = data && data_len && *data_len >= 10 &&
	               data[0] == 0x08 && !(data[4] & 0x01);
	if (txStatus) {
		stat_inc(&s_stats.tx_done_ok);
		if (uc_data) stat_inc(&s_stats.tx_uc_ack);
	} else {
		stat_inc(&s_stats.tx_done_fail);
		if (uc_data) stat_inc(&s_stats.tx_uc_noack);
	}
}

/* Forward one reconstructed MSDU to the data handler and count it (ctx: int).
 * Counting here instead of from the parser's return value keeps rx_msdu exact
 * for an A-MSDU that is cut short: its good leading subframes are delivered
 * before the parser reports the error. Wi-Fi task only, like rx_cb. */
static void deliver_msdu(const uint8_t dst[6], const uint8_t src[6], uint16_t ethertype,
                         const uint8_t *payload, size_t plen, void *ctx) {
	(*(int *)ctx)++;
	if (s_data_handler) s_data_handler(dst, src, ethertype, payload, plen, s_data_ctx);
}

/* ---- promiscuous rx callback: filter AWDL BSSID, parse, enqueue ---- */
static void rx_cb(void *buf, wifi_promiscuous_pkt_type_t type) {
	if (type != WIFI_PKT_MGMT && type != WIFI_PKT_DATA) return;

	const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t *)buf;
	int len = pkt->rx_ctrl.sig_len;
	if (len < (int)(IEEE80211_HDR_LEN + 4)) return;
	len -= 4; /* strip trailing FCS */
	const uint8_t *frame = pkt->payload;

	/* addr3 (BSSID) at offset 16 must be the AWDL BSSID */
	if (memcmp(frame + 16, AWDL_BSSID, 6) != 0) {
		stat_inc(&s_stats.rx_other_bssid);
		return;
	}

	/* Go by the frame-control type bits rather than the driver's label, and
	 * count disagreements (reported on some chips; a check for the C5). */
	wifi_promiscuous_pkt_type_t ftype;
	switch (frame[0] & 0x0c) {
	case 0x00: ftype = WIFI_PKT_MGMT; break;
	case 0x08: ftype = WIFI_PKT_DATA; break;
	default:   stat_inc(&s_stats.rx_type_mismatch); return;
	}
	if (ftype != type) stat_inc(&s_stats.rx_type_mismatch);

	if (ftype == WIFI_PKT_MGMT) {
		/* Is sig_len really MPDU + FCS on this chip? The 4 bytes we strip must
		 * be the CRC-32 of the frame (esp_rom_crc32_le is the IEEE CRC-32).
		 * Action frames only: rare and short, so the CRC is cheap. */
		uint32_t fcs = (uint32_t)frame[len] | ((uint32_t)frame[len + 1] << 8) |
		               ((uint32_t)frame[len + 2] << 16) | ((uint32_t)frame[len + 3] << 24);
		stat_inc(esp_rom_crc32_le(0, frame, (uint32_t)len) == fcs
		         ? &s_stats.rx_fcs_ok : &s_stats.rx_fcs_bad);

		awdl_peer_view_t pv;
		int r = awdl_parse_action(frame, (size_t)len, &pv);
		if (r != AWDL_OK) { stat_inc(&s_stats.rx_parse_err); return; }
		stat_inc(&s_stats.rx_action);

		/* Hand the parsed view to the registered consumer (the awdl task). The
		 * promiscuous callback runs in Wi-Fi task context, so the handler must
		 * be quick and non-blocking (copy into a queue and return). */
		if (s_action_handler)
			s_action_handler(&pv, pkt->rx_ctrl.rssi, pkt->rx_ctrl.channel,
			                 esp_timer_get_time(), s_action_ctx);
	} else { /* WIFI_PKT_DATA */
		/* Only data frames that carry a body (plain/QoS data); null frames etc.
		 * are ignored before they can touch the duplicate cache. */
		bool amsdu;
		if (!awdl_data_hdr_len(frame, (size_t)len, &amsdu)) return;
		/* Promiscuous capture sees every 802.11 retry of a frame; a normal
		 * receiver would discard those, so do the same before forwarding. */
		if (awdl_dedup_is_dup(&s_dedup, frame, (size_t)len)) {
			stat_inc(&s_stats.rx_dup);
			return;
		}
		/* Reconstruct Ethernet per MSDU (one for a plain frame, several for an
		 * A-MSDU) and hand each to the consumer via deliver_msdu. */
		int delivered = 0;
		int r = awdl_parse_data_msdus(frame, (size_t)len, deliver_msdu, &delivered);
		if (delivered > 0) {
			stat_inc(&s_stats.rx_data);
			if (amsdu) stat_inc(&s_stats.rx_amsdu);
			portENTER_CRITICAL(&s_stats_mux);
			s_stats.rx_msdu += (uint64_t)delivered;
			portEXIT_CRITICAL(&s_stats_mux);
		}
		if (r < 0) stat_inc(&s_stats.rx_data_perr);
	}
}

/* ------------------------------------------------------------------------ */

bool radio_channel_supported(uint8_t ch) {
	if (ch >= 1 && ch <= 14) return true;
#if SOC_WIFI_SUPPORT_5G
	if (ch >= 36 && ch <= 48 && ch % 4 == 0) return true;   /* UNII-1, no DFS */
	if (ch >= 149 && ch <= 177 && ch % 4 == 1) return true; /* UNII-3 / SRD */
#endif
	return false;
}

static int8_t tx_power_for(uint8_t ch) {
	return (ch >= 149 && s_txpwr_user > TXPWR_SRD_CAP_QDBM) ? TXPWR_SRD_CAP_QDBM
	                                                         : s_txpwr_user;
}

/* Switch channel with the matching TX power: lower the ceiling before entering
 * the capped band, raise it only after leaving it. */
static esp_err_t set_channel_and_power(uint8_t ch) {
	int8_t want = tx_power_for(ch);
	int8_t now = 0;
	bool lower_first = esp_wifi_get_max_tx_power(&now) != ESP_OK || want < now;
	if (lower_first) esp_wifi_set_max_tx_power(want);
	esp_err_t err = esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
	if (err != ESP_OK) {
		/* still on the old channel: restore its ceiling */
		esp_wifi_set_max_tx_power(tx_power_for(s_channel));
		return err;
	}
	s_channel = ch;
	if (!lower_first) esp_wifi_set_max_tx_power(want);
	return ESP_OK;
}

/*
 * Everything that has to be (re)applied after esp_wifi_start(): power save off
 * (we must stay on-channel to hear/inject AWDL), country, on a dual-band chip
 * both bands and 20 MHz (set_bandwidth is refused in AUTO band mode; AWDL runs
 * at 20 MHz), the tx-done hook, then channel and TX power.
 */
static esp_err_t apply_rf(uint8_t channel) {
	esp_wifi_set_ps(WIFI_PS_NONE);
	if (CONFIG_ESPDROP_WIFI_COUNTRY[0] != '\0') {
		esp_err_t e = esp_wifi_set_country_code(CONFIG_ESPDROP_WIFI_COUNTRY, false);
		if (e != ESP_OK)
			ESP_LOGW(TAG, "country %s: %s", CONFIG_ESPDROP_WIFI_COUNTRY, esp_err_to_name(e));
	}
#if SOC_WIFI_SUPPORT_5G
	/* also guards against a 2.4G-only band mode left in NVS by older firmware */
	esp_err_t e = esp_wifi_set_band_mode(WIFI_BAND_MODE_AUTO);
	if (e != ESP_OK) ESP_LOGW(TAG, "band mode: %s", esp_err_to_name(e));
	wifi_bandwidths_t bw = { .ghz_2g = WIFI_BW20, .ghz_5g = WIFI_BW20 };
	e = esp_wifi_set_bandwidths(WIFI_IF_STA, &bw);
	if (e != ESP_OK) ESP_LOGW(TAG, "bandwidths: %s", esp_err_to_name(e));
#endif
	esp_wifi_set_tx_done_cb(tx_done_cb);
	esp_err_t err = set_channel_and_power(channel);
	if (err != ESP_OK)
		ESP_LOGE(TAG, "channel %u: %s", channel, esp_err_to_name(err));
	return err;
}

int radio_init(uint8_t channel) {
	awdl_dedup_init(&s_dedup);   /* before promiscuous RX can run */
	esp_err_t err = nvs_flash_init();
	if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
		nvs_flash_erase();
		err = nvs_flash_init();
	}
	if (err != ESP_OK) return err;

	/* The Wi-Fi driver posts its events (STA_START etc.) to the default event
	 * loop. Without it they fail with ESP_ERR_INVALID_STATE ("failed to post
	 * WiFi event"). esp_netif is not strictly needed for raw/promiscuous use
	 * but is cheap and required once we add an IP stack later. */
	if ((err = esp_netif_init()) != ESP_OK) return err;
	err = esp_event_loop_create_default();
	if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) return err; /* already created: fine */

	wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
	if ((err = esp_wifi_init(&cfg)) != ESP_OK) return err;
	if ((err = esp_wifi_set_storage(WIFI_STORAGE_RAM)) != ESP_OK) return err;
	if ((err = esp_wifi_set_mode(WIFI_MODE_STA)) != ESP_OK) return err;
	if ((err = esp_wifi_start()) != ESP_OK) return err;

	if ((err = apply_rf(channel)) != ESP_OK) return err;

	uint8_t mac[6];
	esp_wifi_get_mac(WIFI_IF_STA, mac);
	ESP_LOGI(TAG, "ready on ch %u, STA MAC %02x:%02x:%02x:%02x:%02x:%02x",
	         channel, mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
	return ESP_OK;
}

void radio_get_self_mac(uint8_t out[6]) {
	esp_wifi_get_mac(WIFI_IF_STA, out);
}

int radio_set_self_mac(const uint8_t mac[6]) {
	if (mac[0] & 0x01) return ESP_ERR_INVALID_ARG; /* must be unicast */
	esp_err_t err;
	bool was_promisc = s_promisc;
	if (was_promisc) radio_promisc(false);
	if ((err = esp_wifi_stop()) != ESP_OK) return err;
	esp_err_t mac_err = esp_wifi_set_mac(WIFI_IF_STA, mac);
	/* restart either way, with everything re-applied, before reporting */
	if ((err = esp_wifi_start()) != ESP_OK) return err;
	err = apply_rf(s_channel);
	if (was_promisc) radio_promisc(true);
	return mac_err != ESP_OK ? mac_err : err;
}

int radio_set_channel(uint8_t channel) {
	if (!radio_channel_supported(channel)) return ESP_ERR_INVALID_ARG;
	return set_channel_and_power(channel);
}

uint8_t radio_get_channel(void) { return s_channel; }

int radio_set_max_tx_power(int8_t qdbm) {
	if (qdbm < 8 || qdbm > 84) return ESP_ERR_INVALID_ARG;
	s_txpwr_user = qdbm;
	return esp_wifi_set_max_tx_power(tx_power_for(s_channel));
}

int8_t radio_get_max_tx_power(void) {
	int8_t p = 0;
	return esp_wifi_get_max_tx_power(&p) == ESP_OK ? p : -1;
}

int radio_tx(const uint8_t *buf, int len) {
	esp_err_t err = esp_wifi_80211_tx(WIFI_IF_STA, buf, len, false /* keep our seq */);
	if (err == ESP_OK) stat_inc(&s_stats.tx_ok);
	else               stat_inc(&s_stats.tx_err);
	return err;
}

int radio_promisc(bool on) {
	esp_err_t err;
	if (on) {
		wifi_promiscuous_filter_t f = {
			.filter_mask = WIFI_PROMIS_FILTER_MASK_MGMT | WIFI_PROMIS_FILTER_MASK_DATA,
		};
		esp_wifi_set_promiscuous_filter(&f);
		esp_wifi_set_promiscuous_rx_cb(rx_cb);
		if ((err = esp_wifi_set_promiscuous(true)) != ESP_OK) return err;
		/* promiscuous resets the channel; reapply */
		if ((err = esp_wifi_set_channel(s_channel, WIFI_SECOND_CHAN_NONE)) != ESP_OK)
			ESP_LOGW(TAG, "re-applying channel %u: %s", s_channel, esp_err_to_name(err));
	} else {
		if ((err = esp_wifi_set_promiscuous(false)) != ESP_OK) return err;
	}
	s_promisc = on;
	return ESP_OK;
}

bool radio_promisc_enabled(void) { return s_promisc; }

void radio_get_stats(radio_stats_t *out) {
	portENTER_CRITICAL(&s_stats_mux);
	*out = s_stats;
	portEXIT_CRITICAL(&s_stats_mux);
}

void radio_reset_stats(void) {
	portENTER_CRITICAL(&s_stats_mux);
	memset(&s_stats, 0, sizeof(s_stats));
	portEXIT_CRITICAL(&s_stats_mux);
}

void radio_set_action_handler(radio_action_handler_t h, void *ctx) {
	s_action_ctx = ctx;
	s_action_handler = h;
}

void radio_set_data_handler(radio_data_handler_t h, void *ctx) {
	s_data_ctx = ctx;
	s_data_handler = h;
}
