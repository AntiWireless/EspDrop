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

/* How often we emit our own frames and clean the peer table. PSF timing is not
 * critical for M1 (sync is receive-driven); these just keep us advertising. */
#define PSF_INTERVAL_US (AWDL_PSF_INTERVAL_TU * AWDL_TU_US)           /* ~112 ms */
#define MIF_INTERVAL_US (6 * AWDL_PSF_INTERVAL_TU * AWDL_TU_US)       /* ~675 ms */
#define TICK_INTERVAL_US AWDL_PEER_CLEAN_INTERVAL_US                  /* 1 s */
#define RX_Q_LEN 16

typedef struct {
	awdl_peer_view_t pv;
	int8_t   rssi;
	uint8_t  channel;
	uint64_t now_us;
} rx_event_t;

static awdl_node_t      s_node;
static SemaphoreHandle_t s_lock;     /* guards s_node */
static QueueHandle_t    s_rx_q;
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

static void send_frame(uint8_t subtype) {
	/* Static (not on the stack): send_frame runs on both the awdl task and the
	 * console task, and a 1500-byte stack frame would risk overflowing either.
	 * The mutex serialises use of the shared buffer and the node. */
	static uint8_t buf[1500];
	xSemaphoreTake(s_lock, portMAX_DELAY);
	int n = awdl_node_build_action(&s_node, buf, sizeof(buf), subtype,
	                               (uint64_t)esp_timer_get_time());
	if (n > 0)
		radio_tx(buf, n);
	xSemaphoreGive(s_lock);
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
		if (now >= next_psf) {
			send_frame(AWDL_SUBTYPE_PSF);
			next_psf = now + PSF_INTERVAL_US;
		}
		if (now >= next_mif) {
			send_frame(AWDL_SUBTYPE_MIF);
			next_mif = now + MIF_INTERVAL_US;
		}
		if (now >= next_tick) {
			xSemaphoreTake(s_lock, portMAX_DELAY);
			awdl_node_tick(&s_node, (uint64_t)now);
			xSemaphoreGive(s_lock);
			next_tick = now + TICK_INTERVAL_US;
		}
	}
}

int awdl_app_start(const uint8_t self_mac[6], const char *name, uint8_t chan) {
	if (s_started) return 0;

	s_lock = xSemaphoreCreateMutex();
	s_rx_q = xQueueCreate(RX_Q_LEN, sizeof(rx_event_t));
	if (!s_lock || !s_rx_q) return -1;

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
	send_frame(subtype);
}
