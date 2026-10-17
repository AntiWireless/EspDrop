/*
 * EspDrop - AWDL application layer (ESP32). See awdl_app.h.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_app.h"

#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

#include "radio.h"

static const char *TAG = "awdl_app";

/* How often we emit our own frames and clean the peer table. These are upper
 * bounds: send_frame() additionally gates on the advertised slot (see there),
 * so a due frame waits for a slot we advertise as present. Our own sync stays
 * accurate regardless, because it is driven by the master's received frames. */
#define PSF_INTERVAL_US (AWDL_PSF_INTERVAL_TU * AWDL_TU_US)           /* ~112 ms */
#define MIF_INTERVAL_US (6 * AWDL_PSF_INTERVAL_TU * AWDL_TU_US)       /* ~675 ms */
#define TICK_INTERVAL_US AWDL_PEER_CLEAN_INTERVAL_US                  /* 1 s */
#define RX_Q_LEN 16

/* Host->radio data TX: one IPv6 packet (awdl0 MTU) per slot, queued from the
 * bridge RX task and flushed by the AWDL task during advertised-present slots. */
#define TX_Q_LEN        8
#define TX_PAYLOAD_MAX  1484u   /* awdl0 MTU */
#define TX_FLUSH_BUDGET 8       /* data frames sent per ready slot visit */

typedef struct {
	awdl_peer_view_t pv;
	int8_t   rssi;
	uint8_t  channel;
	uint64_t now_us;
} rx_event_t;

typedef struct {
	uint8_t  dst[6];
	uint16_t len;
	uint8_t  ip[TX_PAYLOAD_MAX];
} tx_data_t;

static awdl_node_t      s_node;
static SemaphoreHandle_t s_lock;     /* guards s_node */
static QueueHandle_t    s_rx_q;
static QueueHandle_t    s_tx_q;      /* tx_data_t: bridge RX task -> awdl task */
static TaskHandle_t     s_task;
static bool             s_started;

/* radio handler: Wi-Fi task context, must be quick. Copy into the queue. */
static void on_action(const awdl_peer_view_t *pv, int8_t rssi, uint8_t channel,
                      uint64_t now_us, void *ctx) {
	(void)ctx;
	rx_event_t ev;
	ev.pv = *pv;
	ev.rssi = rssi;
	ev.channel = channel;
	ev.now_us = now_us;
	/* drop on overflow rather than block the Wi-Fi task */
	(void)xQueueSend(s_rx_q, &ev, 0);
}

/*
 * Build and transmit a PSF/MIF. When force is false, the frame is only sent if
 * the current EAW slot is one we advertise presence in (so we do not announce
 * ourselves in slots our own sequence marks absent, and our frames land when
 * the master is on our channel). Returns true if a frame was actually sent.
 * The mutex is NOT recursive, so the slot check happens under the same single
 * lock acquisition as the build. The 1500-byte buffer is static because this
 * runs on both the awdl task and the console task, and a stack frame that large
 * would risk overflowing either.
 */
static bool send_frame(uint8_t subtype, bool force) {
	static uint8_t buf[1500];
	bool sent = false;
	uint64_t now = (uint64_t)esp_timer_get_time();
	xSemaphoreTake(s_lock, portMAX_DELAY);
	if (force || awdl_node_tx_slot_ready(&s_node, now)) {
		int n = awdl_node_build_action(&s_node, buf, sizeof(buf), subtype, now);
		if (n > 0) {
			radio_tx(buf, n);
			sent = true;
		}
	}
	xSemaphoreGive(s_lock);
	return sent;
}

static void awdl_task(void *arg) {
	(void)arg;
	int64_t now = esp_timer_get_time();
	int64_t next_psf = now + PSF_INTERVAL_US;
	int64_t next_mif = now + MIF_INTERVAL_US;
	int64_t next_tick = now + TICK_INTERVAL_US;

	for (;;) {
		rx_event_t ev;
		if (xQueueReceive(s_rx_q, &ev, pdMS_TO_TICKS(20)) == pdTRUE) {
			xSemaphoreTake(s_lock, portMAX_DELAY);
			awdl_node_on_rx_action(&s_node, &ev.pv, ev.rssi, ev.now_us);
			xSemaphoreGive(s_lock);
		}

		now = esp_timer_get_time();
		/* Only advance the timer once the frame actually went out; if the
		 * current slot is one we advertise as absent, retry on the next loop
		 * (~20 ms) until we reach an advertised-present slot. */
		if (now >= next_psf && send_frame(AWDL_SUBTYPE_PSF, false))
			next_psf = now + PSF_INTERVAL_US;
		if (now >= next_mif && send_frame(AWDL_SUBTYPE_MIF, false))
			next_mif = now + MIF_INTERVAL_US;
		if (now >= next_tick) {
			xSemaphoreTake(s_lock, portMAX_DELAY);
			awdl_node_tick(&s_node, (uint64_t)now);
			xSemaphoreGive(s_lock);
			next_tick = now + TICK_INTERVAL_US;
		}

		/* Flush queued host data frames, but only during a slot we advertise as
		 * present (= the master's ch6 slots), so our data TX lands when the peer
		 * is on our channel. Build+send under the same lock that guards self. */
		if (s_tx_q && uxQueueMessagesWaiting(s_tx_q) > 0) {
			static uint8_t txbuf[1600];
			tx_data_t td;
			now = esp_timer_get_time();
			xSemaphoreTake(s_lock, portMAX_DELAY);
			if (awdl_node_tx_slot_ready(&s_node, (uint64_t)now)) {
				int budget = TX_FLUSH_BUDGET;
				while (budget-- > 0 && xQueueReceive(s_tx_q, &td, 0) == pdTRUE) {
					int n = awdl_build_data(txbuf, sizeof(txbuf), &s_node.self,
					                        td.dst, td.ip, td.len);
					if (n > 0) radio_tx(txbuf, n);
				}
			}
			xSemaphoreGive(s_lock);
		}
	}
}

int awdl_app_tx_ethernet(const uint8_t *eth, size_t len, void *ctx) {
	(void)ctx;
	/* Called only from the single bridge RX task, so a static staging buffer is
	 * safe and keeps this large struct off that task's stack. */
	static tx_data_t td;
	if (!s_started || !s_tx_q || len < 14) return -1;
	uint16_t et = (uint16_t)(((uint16_t)eth[12] << 8) | eth[13]);
	if (et != ETHERTYPE_IPV6) return 0;   /* awdl0 is IPv6-only; ignore the rest */
	size_t plen = len - 14;
	if (plen > TX_PAYLOAD_MAX) return -1;
	memcpy(td.dst, eth, 6);
	td.len = (uint16_t)plen;
	if (plen) memcpy(td.ip, eth + 14, plen);
	return (xQueueSend(s_tx_q, &td, 0) == pdTRUE) ? 0 : -1; /* drop on overflow */
}

int awdl_app_start(const uint8_t self_mac[6], const char *name, uint8_t chan) {
	if (s_started) return 0;

	s_lock = xSemaphoreCreateMutex();
	s_rx_q = xQueueCreate(RX_Q_LEN, sizeof(rx_event_t));
	s_tx_q = xQueueCreate(TX_Q_LEN, sizeof(tx_data_t));
	if (!s_lock || !s_rx_q || !s_tx_q) return -1;

	awdl_node_init(&s_node, self_mac, name, chan, (uint64_t)esp_timer_get_time());

	radio_set_action_handler(on_action, NULL);

	if (xTaskCreate(awdl_task, "awdl", 6144, NULL, 5, &s_task) != pdPASS)
		return -1;

	s_started = true;
	ESP_LOGI(TAG, "started on ch %u", chan);
	return 0;
}

void awdl_app_set_self(const uint8_t self_mac[6], uint8_t chan) {
	if (!s_started) return;
	xSemaphoreTake(s_lock, portMAX_DELAY);
	awdl_node_init(&s_node, self_mac, "espdrop-c3", chan,
	               (uint64_t)esp_timer_get_time());
	xSemaphoreGive(s_lock);
}

bool awdl_app_status(awdl_node_status_t *out) {
	if (!s_started) return false;
	xSemaphoreTake(s_lock, portMAX_DELAY);
	awdl_node_status(&s_node, out);
	xSemaphoreGive(s_lock);
	return true;
}

int awdl_app_format_peers(char *buf, size_t cap) {
	if (!s_started || cap == 0) { if (cap) buf[0] = 0; return 0; }
	int off = 0;
	xSemaphoreTake(s_lock, portMAX_DELAY);
	uint64_t now = (uint64_t)esp_timer_get_time();
	for (int i = 0; i < AWDL_MAX_PEERS; i++) {
		const awdl_peer_t *p = &s_node.peers.peers[i];
		if (!p->used) continue;
		int age_ms = (int)((now - p->last_update_us) / 1000);
		off += snprintf(buf + off, (off < (int)cap) ? cap - off : 0,
		                "%02x:%02x:%02x:%02x:%02x:%02x %-10s age %dms seq",
		                p->addr[0], p->addr[1], p->addr[2],
		                p->addr[3], p->addr[4], p->addr[5],
		                p->has_name ? p->name : "?", age_ms);
		for (int s = 0; s < AWDL_CHANSEQ_LEN && p->has_chanseq; s++)
			off += snprintf(buf + off, (off < (int)cap) ? cap - off : 0,
			                " %u", p->chanseq[s]);
		off += snprintf(buf + off, (off < (int)cap) ? cap - off : 0, "\n");
	}
	xSemaphoreGive(s_lock);
	return off;
}

void awdl_app_tx(uint8_t subtype) {
	if (!s_started) return;
	send_frame(subtype, true); /* console `tx`: send now regardless of slot */
}
