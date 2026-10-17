/*
 * EspDrop - USB host-link bridge transport (ESP32). See bridge.h.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "bridge.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

#include "awdl_cobs.h"
#include "awdl_app.h" /* awdl_app_resync_peers, for the `resync` command */
#include "radio.h"    /* radio_get_stats, for the `radio` diagnostic command */

#define BRIDGE_MAX_PAYLOAD 1600
#define BRIDGE_MAX_MSG     (1 + BRIDGE_MAX_PAYLOAD)   /* type + payload */
#define USB_RX_BUF         1024
#define USB_TX_BUF         2048

/* Device->host RX forwarding: one Ethernet frame per queue slot. The Wi-Fi task
 * only builds+enqueues (never blocks on USB); a dedicated task drains and sends. */
#define BRIDGE_RX_PAYLOAD_MAX 1500                 /* L3 payload cap (awdl0 MTU) */
#define BRIDGE_RX_Q_LEN       8
typedef struct {
	uint16_t len;
	uint8_t  eth[14 + BRIDGE_RX_PAYLOAD_MAX];
} rx_frame_t;

/* Peer add/remove notifications, queued by the AWDL task and sent by their own
 * task, so the AWDL timing task never blocks on a USB write. */
#define BRIDGE_PEER_Q_LEN 16
typedef struct {
	bool    added;
	uint8_t mac[6];
} peer_evt_t;

static SemaphoreHandle_t s_tx_lock;
static QueueHandle_t     s_rx_fwd_q;   /* rx_frame_t: Wi-Fi task -> fwd task */
static QueueHandle_t     s_peer_q;     /* peer_evt_t: AWDL task -> peer task */
static bool s_started;

/* Counters touched only from the single RX task (handle_msg), so no locking. */
static uint32_t s_frames_from_host;
static uint32_t s_bytes_from_host;
/* Touched across tasks (written by Wi-Fi/fwd tasks, read by the stats command).
 * 32-bit aligned loads/stores are atomic on the C3, and a stale stats read is
 * harmless, so these are volatile rather than locked. */
static volatile uint32_t s_frames_to_host;
static volatile uint32_t s_rx_drops;

/* Sink for host->device Ethernet frames (set by app_main to the AWDL TX path). */
static bridge_frame_sink_t s_frame_sink;
static void               *s_frame_sink_ctx;

void bridge_set_frame_sink(bridge_frame_sink_t fn, void *ctx) {
	s_frame_sink_ctx = ctx;
	s_frame_sink = fn;
}

/* Emit one synthetic Ethernet frame to the host so the device->host->awdl0 path
 * can be exercised before any radio exists (M2.1). ff:ff:ff:ff:ff:ff <- our MAC,
 * ethertype 0x88b5 (IEEE local experimental, inert) + a marker body. */
static void send_test_frame(void) {
	uint8_t mac[6] = {0};
	esp_read_mac(mac, ESP_MAC_WIFI_STA);
	static const char body[] = "EspDrop M2.1 testframe";
	uint8_t eth[14 + sizeof(body) - 1];
	memset(eth, 0xff, 6);          /* destination: broadcast */
	memcpy(eth + 6, mac, 6);       /* source: our Wi-Fi MAC */
	eth[12] = 0x88;
	eth[13] = 0xb5;                /* ethertype 0x88b5 */
	memcpy(eth + 14, body, sizeof(body) - 1);
	bridge_send(BRIDGE_MSG_FRAME, eth, sizeof(eth));
}

/* Wi-Fi task context: build the Ethernet frame into a scratch slot and enqueue
 * it for the forwarding task. rx_cb is serialized (one Wi-Fi task), so a single
 * static scratch buffer is safe and keeps this off the Wi-Fi task's stack. */
void bridge_rx_frame(const uint8_t dst[6], const uint8_t src[6], uint16_t ethertype,
                     const uint8_t *payload, size_t plen, void *ctx) {
	(void)ctx;
	static rx_frame_t scratch;
	if (!s_rx_fwd_q) return;
	if (plen > BRIDGE_RX_PAYLOAD_MAX) { s_rx_drops++; return; }
	memcpy(scratch.eth, dst, 6);
	memcpy(scratch.eth + 6, src, 6);
	scratch.eth[12] = (uint8_t)(ethertype >> 8);
	scratch.eth[13] = (uint8_t)(ethertype & 0xff);
	if (plen) memcpy(scratch.eth + 14, payload, plen);
	scratch.len = (uint16_t)(14 + plen);
	if (xQueueSend(s_rx_fwd_q, &scratch, 0) != pdTRUE)
		s_rx_drops++; /* host not keeping up: drop rather than stall Wi-Fi */
}

/* Drain the RX-forward queue to the host. A dedicated task so the (bounded but
 * blocking) USB write never runs on the Wi-Fi task or the AWDL timing task. */
static void bridge_fwd_task(void *arg) {
	(void)arg;
	static rx_frame_t fr;
	for (;;) {
		if (xQueueReceive(s_rx_fwd_q, &fr, portMAX_DELAY) == pdTRUE) {
			if (bridge_send(BRIDGE_MSG_FRAME, fr.eth, fr.len) == 0)
				s_frames_to_host++;
		}
	}
}

void bridge_notify_peer(bool added, const uint8_t mac[6]) {
	if (!s_peer_q) return;
	peer_evt_t ev;
	ev.added = added;
	memcpy(ev.mac, mac, 6);
	/* Never block the caller (the AWDL task). On overflow the event is dropped;
	 * the host recovers the full peer set with `resync`. */
	(void)xQueueSend(s_peer_q, &ev, 0);
}

/* Drain peer notifications to the host (USB write off the AWDL timing task). */
static void bridge_peer_task(void *arg) {
	(void)arg;
	peer_evt_t ev;
	for (;;) {
		if (xQueueReceive(s_peer_q, &ev, portMAX_DELAY) == pdTRUE)
			bridge_send(ev.added ? BRIDGE_MSG_PEER_ADD : BRIDGE_MSG_PEER_REMOVE,
			            ev.mac, 6);
	}
}

/* Send a NUL-terminated string as a CMD_REPLY (length from strlen, so a
 * truncated snprintf can never be reported as longer than the buffer). */
static void cmd_reply(const char *s) {
	bridge_send(BRIDGE_MSG_CMD_REPLY, s, strlen(s));
}

/* A short text command from the host (BRIDGE_MSG_CMD) -> a CMD_REPLY. */
static void handle_cmd(const uint8_t *p, size_t n) {
	char cmd[64];
	size_t k = n < sizeof(cmd) - 1 ? n : sizeof(cmd) - 1;
	memcpy(cmd, p, k);
	cmd[k] = '\0';
	while (k && (cmd[k - 1] == '\n' || cmd[k - 1] == '\r' || cmd[k - 1] == ' '))
		cmd[--k] = '\0';

	char out[128];
	if (strcmp(cmd, "ping") == 0) {
		cmd_reply("pong");
	} else if (strcmp(cmd, "mac") == 0) {
		uint8_t mac[6] = {0};
		esp_read_mac(mac, ESP_MAC_WIFI_STA);
		snprintf(out, sizeof(out), "mac %02x:%02x:%02x:%02x:%02x:%02x",
		         mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
		cmd_reply(out);
	} else if (strcmp(cmd, "stats") == 0) {
		snprintf(out, sizeof(out),
		         "frames_from_host=%u bytes_from_host=%u frames_to_host=%u rx_drops=%u",
		         (unsigned)s_frames_from_host, (unsigned)s_bytes_from_host,
		         (unsigned)s_frames_to_host, (unsigned)s_rx_drops);
		cmd_reply(out);
	} else if (strcmp(cmd, "radio") == 0) {
		radio_stats_t st;
		radio_get_stats(&st);
		char rbuf[200];
		/* tx_ack = unicast frames the peer hardware-ACKed (our TX reached it);
		 * for broadcast/multicast tx_ack is always success. Counts are cast to
		 * 32-bit (ample for a test run) and printed with %u, matching `stats`. */
		snprintf(rbuf, sizeof(rbuf),
		         "rx_action=%u rx_data=%u rx_dup=%u rx_other=%u rx_perr=%u "
		         "tx_ok=%u tx_err=%u tx_ack=%u tx_noack=%u",
		         (unsigned)st.rx_action, (unsigned)st.rx_data, (unsigned)st.rx_dup,
		         (unsigned)st.rx_other_bssid, (unsigned)st.rx_parse_err,
		         (unsigned)st.tx_ok, (unsigned)st.tx_err,
		         (unsigned)st.tx_done_ok, (unsigned)st.tx_done_fail);
		cmd_reply(rbuf);
	} else if (strcmp(cmd, "tx") == 0) {
		/* Host->radio data path: did the frame reach the radio, and did the peer
		 * ACK our unicast data frames (broadcast PSF/MIF excluded)? */
		awdl_app_tx_counters_t c;
		awdl_app_get_tx_counters(&c);
		radio_stats_t st;
		radio_get_stats(&st);
		snprintf(out, sizeof(out),
		         "queued=%u q_drop=%u sent_uc=%u sent_mc=%u fail=%u uc_ack=%u uc_noack=%u",
		         (unsigned)c.queued, (unsigned)c.q_drop, (unsigned)c.sent_uc,
		         (unsigned)c.sent_mc, (unsigned)c.tx_fail,
		         (unsigned)st.tx_uc_ack, (unsigned)st.tx_uc_noack);
		cmd_reply(out);
	} else if (strcmp(cmd, "testframe") == 0) {
		send_test_frame();
		cmd_reply("sent testframe");
	} else if (strcmp(cmd, "resync") == 0) {
		/* Re-announce every known peer on the next AWDL tick. The host sends this
		 * on connect, because the C3 keeps running across host sessions (its USB
		 * port does not reset the chip) and would otherwise never re-emit peers
		 * it already knew before the host attached. */
		awdl_app_resync_peers();
		cmd_reply("resync");
	} else {
		snprintf(out, sizeof(out), "unknown cmd '%s'", cmd);
		cmd_reply(out);
	}
}

/* Dispatch a fully decoded message (type byte + payload). */
static void handle_msg(const uint8_t *msg, size_t len) {
	if (len < 1) return;
	uint8_t type = msg[0];
	const uint8_t *payload = msg + 1;
	size_t plen = len - 1;

	switch (type) {
	case BRIDGE_MSG_ECHO:
		bridge_send(BRIDGE_MSG_ECHO_REPLY, payload, plen);
		break;
	case BRIDGE_MSG_FRAME:
		/* Host -> device Ethernet frame for awdl0. Account it, then hand it to the
		 * TX sink (the AWDL data-TX path) if one is registered. */
		s_frames_from_host++;
		s_bytes_from_host += (uint32_t)plen;
		if (s_frame_sink)
			s_frame_sink(payload, plen, s_frame_sink_ctx);
		break;
	case BRIDGE_MSG_CMD:
		handle_cmd(payload, plen);
		break;
	/* PEER_* are device->host only (see bridge_notify_peer). */
	default:
		break;
	}
}

static void bridge_rx_task(void *arg) {
	(void)arg;
	static cobs_rx_t rx;
	static uint8_t out[BRIDGE_MAX_MSG];
	uint8_t chunk[256];
	cobs_rx_init(&rx);

	for (;;) {
		int n = usb_serial_jtag_read_bytes(chunk, sizeof(chunk), portMAX_DELAY);
		if (n <= 0) continue;
		for (int i = 0; i < n; i++) {
			int r = cobs_rx_byte(&rx, chunk[i], out, sizeof(out));
			if (r > 0)
				handle_msg(out, (size_t)r);
			/* r == -1: malformed/overflowed frame, already reset; ignore. */
		}
	}
}

int bridge_start(void) {
	if (s_started) return 0;

	s_tx_lock = xSemaphoreCreateMutex();
	if (!s_tx_lock) return -1;

	usb_serial_jtag_driver_config_t cfg = {
		.tx_buffer_size = USB_TX_BUF,
		.rx_buffer_size = USB_RX_BUF,
	};
	esp_err_t e = usb_serial_jtag_driver_install(&cfg);
	if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) { /* INVALID_STATE: console already installed it */
		vSemaphoreDelete(s_tx_lock);
		s_tx_lock = NULL;
		return e;
	}

	s_rx_fwd_q = xQueueCreate(BRIDGE_RX_Q_LEN, sizeof(rx_frame_t));
	s_peer_q   = xQueueCreate(BRIDGE_PEER_Q_LEN, sizeof(peer_evt_t));
	if (!s_rx_fwd_q || !s_peer_q) {
		if (s_rx_fwd_q) vQueueDelete(s_rx_fwd_q);
		if (s_peer_q)   vQueueDelete(s_peer_q);
		s_rx_fwd_q = NULL;
		s_peer_q = NULL;
		vSemaphoreDelete(s_tx_lock);
		s_tx_lock = NULL;
		return -1;
	}

	/* Reduce corruption of the binary frame stream by silencing ESP_LOG. This is
	 * not absolute: ROM/early-boot output (before this runs) and the panic
	 * handler (esp_rom_printf, crash only) still reach the same peripheral. Those
	 * are pre-stream or crash-only, and COBS resynchronises at the next 0x00
	 * delimiter, so steady-state framing is unaffected. */
	esp_log_level_set("*", ESP_LOG_NONE);

	/* Set started before creating the task: the RX task runs at a higher
	 * priority and may dispatch (and reply to) a queued ECHO before xTaskCreate
	 * returns; bridge_send must not reject that first reply. */
	s_started = true;
	if (xTaskCreate(bridge_rx_task, "bridge_rx", 4096, NULL, 6, NULL) != pdPASS) {
		s_started = false;
		vQueueDelete(s_rx_fwd_q);
		s_rx_fwd_q = NULL;
		vQueueDelete(s_peer_q);
		s_peer_q = NULL;
		vSemaphoreDelete(s_tx_lock);
		s_tx_lock = NULL;
		/* Logging was just silenced; restore it so the caller's error is visible
		 * (nothing was transmitted yet, so no COBS stream to corrupt). */
		esp_log_level_set("*", ESP_LOG_ERROR);
		return -1;
	}
	/* The forwarding task is best-effort: if it fails to start, RX frames are
	 * dropped (queue fills) but the control link stays up, so do not fail. */
	if (xTaskCreate(bridge_fwd_task, "bridge_fwd", 3072, NULL, 5, NULL) != pdPASS)
		bridge_log("warning: rx-forward task did not start");
	if (xTaskCreate(bridge_peer_task, "bridge_peer", 2560, NULL, 4, NULL) != pdPASS)
		bridge_log("warning: peer-notify task did not start");

	bridge_log("bridge up");
	return 0;
}

int bridge_send(uint8_t type, const void *payload, size_t len) {
	if (!s_started || len > BRIDGE_MAX_PAYLOAD) return -1;

	static uint8_t msg[BRIDGE_MAX_MSG];
	static uint8_t enc[COBS_ENCODE_BOUND(BRIDGE_MAX_MSG) + 1];

	xSemaphoreTake(s_tx_lock, portMAX_DELAY);
	msg[0] = type;
	if (len && payload) memcpy(msg + 1, payload, len);
	size_t mlen = 1 + len;
	size_t enc_len = cobs_encode(msg, mlen, enc, sizeof(enc) - 1);
	int ret = -1;
	if (enc_len > 0) {
		enc[enc_len++] = 0x00; /* frame delimiter */
		/* Write the whole frame, looping over short writes. A bounded timeout
		 * keeps a host that stops draining from wedging the lock (and thus every
		 * other sender) forever; on timeout we drop the rest of this frame. */
		size_t off = 0;
		ret = 0;
		while (off < enc_len) {
			int w = usb_serial_jtag_write_bytes(enc + off, enc_len - off,
			                                    pdMS_TO_TICKS(1000));
			if (w <= 0) { ret = -1; break; } /* host not draining: drop */
			off += (size_t)w;
		}
	}
	xSemaphoreGive(s_tx_lock);
	return ret;
}

void bridge_log(const char *fmt, ...) {
	char buf[200];
	va_list ap;
	va_start(ap, fmt);
	int n = vsnprintf(buf, sizeof(buf), fmt, ap);
	va_end(ap);
	if (n < 0) return;
	if (n > (int)sizeof(buf) - 1) n = sizeof(buf) - 1;
	bridge_send(BRIDGE_MSG_LOG, buf, (size_t)n);
}
