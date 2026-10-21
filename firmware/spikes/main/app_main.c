/*
 * EspDrop - AWDL firmware for the ESP32-C3 and ESP32-C5.
 *
 * Two build modes (menuconfig -> EspDrop -> Bridge mode):
 *  - Console mode (default): brings up Wi-Fi + the AWDL node and an interactive
 *    USB-serial console (tx/rx/sync/peers/...). Used for the M0/M1 workflow;
 *    see console.c and docs/SPIKES.md.
 *  - Bridge mode (CONFIG_ESPDROP_BRIDGE_MODE): starts the COBS-framed USB
 *    host-link bridge instead of the console, for the M2 data path. M2.0/M2.1
 *    were transport-only; M2.2 brings up Wi-Fi + the AWDL node and forwards
 *    received AWDL data frames to the host as Ethernet (awdl0 RX path).
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include <stdio.h>

#include "esp_err.h"   /* ESP_OK, used by both the bridge and console branches */
#include "esp_log.h"
#include "sdkconfig.h"
#include "soc/soc_caps.h"

#include "radio.h"

/* AWDL channel at boot (menuconfig, default 6 = the 2.4 GHz social channel and
 * the only one on the C3); falls back to 6 if this chip cannot use it. */
static uint8_t boot_channel(void) {
	uint8_t ch = (uint8_t)CONFIG_ESPDROP_AWDL_CHANNEL;
	if (!radio_channel_supported(ch)) {
		ESP_LOGW("espdrop", "channel %u not supported on this chip, using 6", ch);
		ch = 6;
	}
	return ch;
}

#if CONFIG_ESPDROP_BRIDGE_MODE

#include "bridge.h"
#include "awdl_app.h"

/* AWDL peer appeared/left -> tell the host so it can manage the awdl0 neighbour
 * table. Runs on the AWDL timing task, so it only enqueues (bridge_notify_peer
 * never blocks); the USB write happens on a bridge task. */
static void on_peer_evt(bool added, const uint8_t mac[6], void *ctx) {
	(void)ctx;
	bridge_notify_peer(added, mac);
}

void app_main(void) {
	/* Bring up the radio and AWDL node FIRST: their one-time init logs (incl.
	 * non-maskable PHY/ROM prints) go out at boot, before the host opens the
	 * port. bridge_start() then silences ESP_LOG and takes over the USB endpoint,
	 * so steady-state traffic is clean COBS. */
	uint8_t chan = boot_channel();
	int err = radio_init(chan);
	if (err != ESP_OK) {
		ESP_LOGE("espdrop", "radio_init failed: 0x%x", err);
		return;
	}

	uint8_t mac[6];
	radio_get_self_mac(mac);
	if (awdl_app_start(mac, CONFIG_ESPDROP_AWDL_NAME, chan) != 0) {
		ESP_LOGE("espdrop", "awdl_app_start failed");
		return;
	}

	if (bridge_start() != 0) {
		ESP_LOGE("espdrop", "bridge_start failed");
		return;
	}

	/* RX: forward received AWDL data frames to the host as Ethernet.
	 * TX: hand host Ethernet frames to the AWDL data-TX path. Then start RX. */
	radio_set_data_handler(bridge_rx_frame, NULL);
	bridge_set_frame_sink(awdl_app_tx_ethernet, NULL);
	awdl_app_set_peer_cb(on_peer_evt, NULL);   /* host builds the neighbour table */
#if SOC_WIFI_SUPPORT_5G
	awdl_app_set_auto_channel(true);   /* follow the iPhone onto 44/149 on its own */
#endif
	if (radio_promisc(true) != ESP_OK)
		bridge_log("warning: promiscuous RX did not enable");
	bridge_log("awdl rx/tx up on %s ch %u", CONFIG_IDF_TARGET, chan);
}

#else /* console mode */

#include "esp_console.h"
#include "awdl_app.h"

void console_register_commands(void);

void app_main(void) {
	ESP_LOGI("espdrop", "EspDrop AWDL (console mode) starting");

	uint8_t chan = boot_channel();
	int err = radio_init(chan);
	if (err != ESP_OK) {
		ESP_LOGE("espdrop", "radio_init failed: 0x%x", err);
		return;
	}

	uint8_t mac[6];
	radio_get_self_mac(mac);
	if (awdl_app_start(mac, CONFIG_ESPDROP_AWDL_NAME, chan) != 0)
		ESP_LOGE("espdrop", "awdl_app_start failed");

	esp_console_repl_t *repl = NULL;
	esp_console_repl_config_t repl_cfg = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
	repl_cfg.prompt = "espdrop>";
	repl_cfg.max_cmdline_length = 256;
	repl_cfg.task_stack_size = 8192; /* cmd_txdata/tx build ~1.5 KB frames */

	esp_console_dev_usb_serial_jtag_config_t usb_cfg =
		ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
	ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&usb_cfg, &repl_cfg, &repl));

	esp_console_register_help_command();
	console_register_commands();

	ESP_ERROR_CHECK(esp_console_start_repl(repl));
	ESP_LOGI("espdrop", "console ready; type 'help'");
}

#endif /* CONFIG_ESPDROP_BRIDGE_MODE */
