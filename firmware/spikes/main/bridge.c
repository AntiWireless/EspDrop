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
	if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) /* INVALID_STATE: console already installed it */
		return e;

	/* Nothing else may write to the USB-Serial/JTAG now, or it would corrupt the
	 * binary frame stream. Silence the default log output; visibility goes
	 * through BRIDGE_MSG_LOG instead. */
	esp_log_level_set("*", ESP_LOG_NONE);

	if (xTaskCreate(bridge_rx_task, "bridge_rx", 4096, NULL, 6, NULL) != pdPASS)
		return -1;

	s_started = true;
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
		usb_serial_jtag_write_bytes(enc, enc_len, portMAX_DELAY);
		ret = 0;
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
