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
/* TX queue depth. Kept at 8: outbound traffic while receiving is mostly small
 * TCP ACKs (q_drop stayed 0 in the ping tests), and every entry reserves a full
 * MTU, so the heap is better spent on the device->host RX queue (bridge.c). */
#define TX_Q_LEN 8
/* Max IPv6 payload per data frame. esp_wifi_80211_tx caps the 802.11 frame at
 * 1500 bytes; awdl_build_data adds 40 bytes of framing (24 B 802.11 + 8 B
 * LLC/SNAP + 8 B AWDL data header), so the payload must stay <= 1500 - 40. This
 * is the effective awdl0 MTU (the host TAP is configured to match). */
#define TX_PAYLOAD_MAX  1460u
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
static char             s_name[AWDL_NAME_MAX + 1];  /* kept for awdl_app_set_self */
static volatile bool    s_tx_paused;   /* awdl_app_pause_tx: no PSF/MIF/data TX */

/* Automatic channel: follow the master onto its preferred social channel. Off
 * by default (set on for 5 GHz chips at start); a manual `chan` turns it off. */
static bool             s_auto_chan;
static int64_t          s_auto_next_us;    /* earliest next auto switch (dwell) */
#define AUTO_CHAN_DWELL_US 4000000          /* >= peer timeout, so no flapping */
/* Social channels we may park on, newest Apple set; radio_channel_supported
 * filters out what this chip cannot do (the C3 keeps only 6). */
static const uint8_t    k_social_chans[] = {6, 44, 149};

/* Peer-change notification. Shadow set is touched only by the AWDL task. */
static awdl_peer_cb_t   s_peer_cb;
static void            *s_peer_cb_ctx;
static volatile bool    s_peer_resync;   /* set by set_peer_cb; re-emit all peers */
static uint8_t          s_peer_shadow[AWDL_MAX_PEERS][6];
static int              s_peer_nshadow;

/* Data-TX counters: written by the bridge RX task (queued/q_drop) and the AWDL
 * task (sent/fail), read by the `tx` command. Aligned 32-bit, stats only. */
static volatile uint32_t s_tx_queued, s_tx_q_drop, s_tx_sent_uc, s_tx_sent_mc, s_tx_fail;

/* Preferred park channel from the master's sequence, restricted to the social
 * channels this chip supports. Call with s_lock held (reads s_node). 0 = keep. */
static uint8_t pick_auto_channel(void) {
	uint8_t allowed[sizeof(k_social_chans)];
	int na = 0;
	for (unsigned i = 0; i < sizeof(k_social_chans); i++)
		if (radio_channel_supported(k_social_chans[i]))
			allowed[na++] = k_social_chans[i];
	return awdl_node_preferred_channel(&s_node, allowed, na);
}

/* Retune the radio and the node to a new channel together (node sync is kept).
 * Takes s_lock. Used by the auto-channel logic and the bridge `chan` command. */
static int retune(uint8_t chan) {
	int err = radio_set_channel(chan);
	if (err) return err;
	xSemaphoreTake(s_lock, portMAX_DELAY);
	awdl_node_set_channel(&s_node, chan);
	xSemaphoreGive(s_lock);
	if (s_task) xTaskNotifyGive(s_task);   /* re-derive slots, flush promptly */
	return 0;
}

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
	if (xQueueSend(s_rx_q, &ev, 0) == pdTRUE && s_task)
		xTaskNotifyGive(s_task);
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

/* Diff the current peer set against the previous snapshot and fire s_peer_cb for
 * each addition/removal. Runs on the AWDL task, outside s_lock (the cb may block
 * briefly on a USB write). n is small (<= AWDL_MAX_PEERS), so O(n^2) is fine. */
static void emit_peer_changes(const uint8_t cur[][6], int ncur) {
	if (s_peer_resync) { s_peer_nshadow = 0; s_peer_resync = false; }
	if (s_peer_cb) {
		for (int i = 0; i < s_peer_nshadow; i++) {
			bool found = false;
			for (int j = 0; j < ncur; j++)
				if (memcmp(s_peer_shadow[i], cur[j], 6) == 0) { found = true; break; }
			if (!found) s_peer_cb(false, s_peer_shadow[i], s_peer_cb_ctx);
		}
		for (int j = 0; j < ncur; j++) {
			bool found = false;
			for (int i = 0; i < s_peer_nshadow; i++)
				if (memcmp(s_peer_shadow[i], cur[j], 6) == 0) { found = true; break; }
			if (!found) s_peer_cb(true, cur[j], s_peer_cb_ctx);
		}
	}
	for (int j = 0; j < ncur; j++) memcpy(s_peer_shadow[j], cur[j], 6);
	s_peer_nshadow = ncur;
}

static void awdl_task(void *arg) {
	(void)arg;
	int64_t now = esp_timer_get_time();
	int64_t next_psf = now + PSF_INTERVAL_US;
	int64_t next_mif = now + MIF_INTERVAL_US;
	int64_t next_tick = now + TICK_INTERVAL_US;

	for (;;) {
		/* Both producers (on_action, awdl_app_tx_ethernet) notify this task,
		 * so new work is handled at once; a host frame that arrives late in a
		 * present slot still goes out in that slot. The timeout only paces
		 * retries: 20 ms for a due PSF/MIF, and 2 ms (FREERTOS_HZ=1000) while
		 * host frames wait for the next present slot. */
		TickType_t wait = (s_tx_q && uxQueueMessagesWaiting(s_tx_q) > 0)
		                  ? pdMS_TO_TICKS(2) : pdMS_TO_TICKS(20);
		if (wait == 0) wait = 1;
		(void)ulTaskNotifyTake(pdTRUE, wait);

		rx_event_t ev;
		while (xQueueReceive(s_rx_q, &ev, 0) == pdTRUE) {
			xSemaphoreTake(s_lock, portMAX_DELAY);
			awdl_node_on_rx_action(&s_node, &ev.pv, ev.rssi, ev.now_us);
			xSemaphoreGive(s_lock);
		}

		now = esp_timer_get_time();
		/* Only advance the timer once the frame actually went out; if the
		 * current slot is one we advertise as absent, retry on the next loop
		 * (~20 ms) until we reach an advertised-present slot. */
		if (!s_tx_paused && now >= next_psf && send_frame(AWDL_SUBTYPE_PSF, false))
			next_psf = now + PSF_INTERVAL_US;
		if (!s_tx_paused && now >= next_mif && send_frame(AWDL_SUBTYPE_MIF, false))
			next_mif = now + MIF_INTERVAL_US;
		if (now >= next_tick) {
			uint8_t cur[AWDL_MAX_PEERS][6];
			int ncur = 0;
			uint8_t want_chan = 0;
			xSemaphoreTake(s_lock, portMAX_DELAY);
			awdl_node_tick(&s_node, (uint64_t)now);
			for (int i = 0; i < AWDL_MAX_PEERS; i++)
				if (s_node.peers.peers[i].used)
					memcpy(cur[ncur++], s_node.peers.peers[i].addr, 6);
			if (s_auto_chan && now >= s_auto_next_us) {
				uint8_t best = pick_auto_channel();   /* reads s_node, under lock */
				if (best && best != radio_get_channel()) want_chan = best;
			}
			xSemaphoreGive(s_lock);
			emit_peer_changes(cur, ncur);   /* notify host of peer add/remove */
			if (want_chan) {
				retune(want_chan);          /* takes s_lock itself */
				s_auto_next_us = esp_timer_get_time() + AUTO_CHAN_DWELL_US;
			}
			next_tick = now + TICK_INTERVAL_US;
		}

		/* Flush queued host data frames, but only during a slot we advertise as
		 * present (= the master's ch6 slots), so our data TX lands when the peer
		 * is on our channel. Build+send under the same lock that guards self. */
		if (!s_tx_paused && s_tx_q && uxQueueMessagesWaiting(s_tx_q) > 0) {
			static uint8_t txbuf[1600];
			tx_data_t td;
			now = esp_timer_get_time();
			xSemaphoreTake(s_lock, portMAX_DELAY);
			if (awdl_node_tx_slot_ready(&s_node, (uint64_t)now)) {
				int budget = TX_FLUSH_BUDGET;
				while (budget-- > 0 && xQueueReceive(s_tx_q, &td, 0) == pdTRUE) {
					int n = awdl_build_data(txbuf, sizeof(txbuf), &s_node.self,
					                        td.dst, td.ip, td.len);
					if (n > 0 && radio_tx(txbuf, n) == 0) {
						if (td.dst[0] & 0x01) s_tx_sent_mc++;
						else                  s_tx_sent_uc++;
					} else {
						s_tx_fail++;
					}
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
	if (xQueueSend(s_tx_q, &td, 0) != pdTRUE) { s_tx_q_drop++; return -1; } /* full */
	s_tx_queued++;
	xTaskNotifyGive(s_task);   /* flush now if we are inside a present slot */
	return 0;
}

void awdl_app_get_tx_counters(awdl_app_tx_counters_t *out) {
	out->queued  = s_tx_queued;
	out->q_drop  = s_tx_q_drop;
	out->sent_uc = s_tx_sent_uc;
	out->sent_mc = s_tx_sent_mc;
	out->tx_fail = s_tx_fail;
}

void awdl_app_set_peer_cb(awdl_peer_cb_t cb, void *ctx) {
	s_peer_cb_ctx = ctx;
	s_peer_cb = cb;
	s_peer_resync = true;   /* re-emit all current peers on the next tick */
}

void awdl_app_resync_peers(void) {
	s_peer_resync = true;   /* consumed by emit_peer_changes on the AWDL task */
}

int awdl_app_start(const uint8_t self_mac[6], const char *name, uint8_t chan) {
	if (s_started) return 0;

	s_lock = xSemaphoreCreateMutex();
	s_rx_q = xQueueCreate(RX_Q_LEN, sizeof(rx_event_t));
	s_tx_q = xQueueCreate(TX_Q_LEN, sizeof(tx_data_t));
	if (!s_lock || !s_rx_q || !s_tx_q) return -1;

	if (name) strncpy(s_name, name, sizeof(s_name) - 1);
	awdl_node_init(&s_node, self_mac, s_name, chan, (uint64_t)esp_timer_get_time());

	/* Task first: on_action notifies it (and skips that while s_task is unset). */
	if (xTaskCreate(awdl_task, "awdl", 6144, NULL, 5, &s_task) != pdPASS)
		return -1;

	radio_set_action_handler(on_action, NULL);

	s_started = true;
	ESP_LOGI(TAG, "started on ch %u", chan);
	return 0;
}

void awdl_app_set_self(const uint8_t self_mac[6], uint8_t chan) {
	if (!s_started) return;
	xSemaphoreTake(s_lock, portMAX_DELAY);
	awdl_node_init(&s_node, self_mac, s_name, chan, (uint64_t)esp_timer_get_time());
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

int awdl_app_set_channel(uint8_t chan, bool keep_auto) {
	if (!s_started) return -1;
	if (!keep_auto) s_auto_chan = false;   /* a manual channel pins it */
	int err = retune(chan);
	if (!err) s_auto_next_us = esp_timer_get_time() + AUTO_CHAN_DWELL_US;
	return err;
}

void awdl_app_set_auto_channel(bool on) {
	s_auto_chan = on;
	s_auto_next_us = 0;   /* act on the next tick */
}

bool awdl_app_auto_channel(void) { return s_auto_chan; }

void awdl_app_pause_tx(bool paused) {
	s_tx_paused = paused;
	if (!paused && s_task) xTaskNotifyGive(s_task);   /* catch up on due frames */
}

void awdl_app_tx(uint8_t subtype) {
	if (!s_started) return;
	send_frame(subtype, true); /* console `tx`: send now regardless of slot */
}
