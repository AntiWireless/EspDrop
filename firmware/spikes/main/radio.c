/*
 * EspDrop - ESP32 Wi-Fi radio glue for the AWDL spikes.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "radio.h"

#include <string.h>

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_private/wifi.h" /* esp_wifi_set_tx_done_cb: stable across IDF 5.1..6.x */
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

static const char *TAG = "radio";

static radio_stats_t   s_stats;
static portMUX_TYPE    s_stats_mux = portMUX_INITIALIZER_UNLOCKED;
static QueueHandle_t   s_peer_q;      /* radio_peer_event_t */
static volatile bool   s_promisc;
static uint8_t         s_channel = 6;

#define PEER_Q_LEN 16

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
	(void)ifidx; (void)data; (void)data_len;
	if (txStatus)
		stat_inc(&s_stats.tx_done_ok);
	else
		stat_inc(&s_stats.tx_done_fail);
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
		radio_peer_event_t ev;
		awdl_peer_view_t pv;
		int r = awdl_parse_action(frame, (size_t)len, &pv);
		if (r != AWDL_OK) { stat_inc(&s_stats.rx_parse_err); return; }
		stat_inc(&s_stats.rx_action);

		memset(&ev, 0, sizeof(ev));
		memcpy(ev.addr, pv.src_addr, 6);
		ev.rssi        = pkt->rx_ctrl.rssi;
		ev.channel     = pkt->rx_ctrl.channel;
		ev.subtype     = pv.subtype;
		ev.has_name    = pv.has_name;
		if (pv.has_name) memcpy(ev.name, pv.name, sizeof(ev.name));
		ev.has_chanseq = pv.has_chanseq;
		if (pv.has_chanseq) memcpy(ev.chanseq, pv.chanseq, sizeof(ev.chanseq));
		ev.rx_timestamp = pkt->rx_ctrl.timestamp;
		ev.esp_timer_us = esp_timer_get_time();

		if (s_peer_q) {
			BaseType_t hp = pdFALSE;
			/* drop oldest on overflow so the newest always gets through */
			if (xQueueSendFromISR(s_peer_q, &ev, &hp) != pdTRUE) {
				radio_peer_event_t drop;
				xQueueReceiveFromISR(s_peer_q, &drop, &hp);
				xQueueSendFromISR(s_peer_q, &ev, &hp);
			}
			if (hp) portYIELD_FROM_ISR();
		}
	} else { /* WIFI_PKT_DATA */
		size_t poff; uint16_t et;
		if (awdl_parse_data(frame, (size_t)len, &poff, &et))
			stat_inc(&s_stats.rx_data);
	}
}

/* ------------------------------------------------------------------------ */

int radio_init(uint8_t channel) {
	esp_err_t err = nvs_flash_init();
	if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
		nvs_flash_erase();
		err = nvs_flash_init();
	}
	if (err != ESP_OK) return err;

	s_peer_q = xQueueCreate(PEER_Q_LEN, sizeof(radio_peer_event_t));
	if (!s_peer_q) return ESP_ERR_NO_MEM;

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

bool radio_poll_peer_event(radio_peer_event_t *ev) {
	if (!s_peer_q) return false;
	return xQueueReceive(s_peer_q, ev, 0) == pdTRUE;
}
