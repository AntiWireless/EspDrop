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
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

#include "awdl_cobs.h"

#define BRIDGE_MAX_PAYLOAD 1600
#define BRIDGE_MAX_MSG     (1 + BRIDGE_MAX_PAYLOAD)   /* type + payload */
#define USB_RX_BUF         1024
#define USB_TX_BUF         2048

static SemaphoreHandle_t s_tx_lock;
static bool s_started;

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
	/* FRAME / CMD / PEER_* are wired up in later M2 stages. */
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
		vSemaphoreDelete(s_tx_lock);
		s_tx_lock = NULL;
		return -1;
	}

	bridge_log("bridge up (M2.0 echo)");
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
