/*
 * EspDrop - AWDL firmware for the ESP32-C3.
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

#include "esp_log.h"
#include "sdkconfig.h"

#define DEFAULT_CHANNEL 6  /* AWDL 2.4 GHz social channel, the only one on the C3 */

#if CONFIG_ESPDROP_BRIDGE_MODE

#include "bridge.h"
#include "radio.h"
#include "awdl_app.h"

void app_main(void) {
	/* Bring up the radio and AWDL node FIRST: their one-time init logs (incl.
	 * non-maskable PHY/ROM prints) go out at boot, before the host opens the
	 * port. bridge_start() then silences ESP_LOG and takes over the USB endpoint,
	 * so steady-state traffic is clean COBS. */
	int err = radio_init(DEFAULT_CHANNEL);
	if (err != ESP_OK) {
		ESP_LOGE("espdrop", "radio_init failed: 0x%x", err);
		return;
	}

	uint8_t mac[6];
	radio_get_self_mac(mac);
	if (awdl_app_start(mac, "espdrop-c3", DEFAULT_CHANNEL) != 0) {
		ESP_LOGE("espdrop", "awdl_app_start failed");
		return;
	}

	if (bridge_start() != 0) {
		ESP_LOGE("espdrop", "bridge_start failed");
		return;
	}

	/* Forward received AWDL data frames to the host as Ethernet, then start RX. */
	radio_set_data_handler(bridge_rx_frame, NULL);
	if (radio_promisc(true) != ESP_OK)
		bridge_log("warning: promiscuous RX did not enable");
	bridge_log("awdl rx up on ch %u (M2.2)", DEFAULT_CHANNEL);
}

#else /* console mode */

#include "esp_console.h"
#include "radio.h"
#include "awdl_app.h"

void console_register_commands(void);

void app_main(void) {
	ESP_LOGI("espdrop", "EspDrop AWDL (console mode) starting");

	int err = radio_init(DEFAULT_CHANNEL);
	if (err != ESP_OK) {
		ESP_LOGE("espdrop", "radio_init failed: 0x%x", err);
		return;
	}

	uint8_t mac[6];
	radio_get_self_mac(mac);
	if (awdl_app_start(mac, "espdrop-c3", DEFAULT_CHANNEL) != 0)
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
