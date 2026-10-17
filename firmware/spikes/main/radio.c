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
#include "nvs_flash.h"
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

	if (type == WIFI_PKT_MGMT) {
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
		size_t poff; uint16_t et;
		if (awdl_parse_data(frame, (size_t)len, &poff, &et)) {
			/* Promiscuous capture sees every 802.11 retry of a frame; a normal
			 * receiver would discard those, so do the same before forwarding. */
			if (awdl_dedup_is_dup(&s_dedup, frame, (size_t)len)) {
				stat_inc(&s_stats.rx_dup);
				return;
			}
			stat_inc(&s_stats.rx_data);
			/* Reconstruct Ethernet (dst=addr1, src=addr2) and hand it to the
			 * consumer. addr1/addr2 sit at the fixed offsets 4/10, and the BSSID
			 * check above already guaranteed len >= IEEE80211_HDR_LEN + 4. */
			if (s_data_handler && poff <= (size_t)len)
				s_data_handler(frame + 4, frame + 10, et,
				               frame + poff, (size_t)len - poff, s_data_ctx);
		}
	}
}

/* ------------------------------------------------------------------------ */

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

	/* no power save: we must stay on-channel to hear/inject AWDL */
	esp_wifi_set_ps(WIFI_PS_NONE);
	esp_wifi_set_tx_done_cb(tx_done_cb);

	s_channel = channel;
	esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);

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
	if ((err = esp_wifi_set_mac(WIFI_IF_STA, mac)) != ESP_OK) { esp_wifi_start(); return err; }
	if ((err = esp_wifi_start()) != ESP_OK) return err;
	esp_wifi_set_ps(WIFI_PS_NONE);
	esp_wifi_set_channel(s_channel, WIFI_SECOND_CHAN_NONE);
	if (was_promisc) radio_promisc(true);
	return ESP_OK;
}

int radio_set_channel(uint8_t channel) {
	esp_err_t err = esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
	if (err == ESP_OK) s_channel = channel;
	return err;
}

uint8_t radio_get_channel(void) { return s_channel; }

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
		esp_wifi_set_channel(s_channel, WIFI_SECOND_CHAN_NONE);
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
