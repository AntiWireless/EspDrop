/*
 * EspDrop - AWDL hardware spikes for the ESP32-C3.
 *
 * Milestone M0: prove the ESP32-C3 radio can do what AWDL needs. Brings up
 * Wi-Fi, starts a USB-serial console, and lets you inject AWDL action/data
 * frames, sniff AWDL frames from nearby Apple devices, and measure channel
 * switch latency and USB throughput. See console.c for the command list and
 * docs/SPIKES.md for the test plan.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include <stdio.h>

#include "esp_console.h"
#include "esp_log.h"

#include "radio.h"

#define DEFAULT_CHANNEL 6  /* AWDL 2.4 GHz social channel, the only one on the C3 */

void console_register_commands(void);

void app_main(void) {
	ESP_LOGI("espdrop", "EspDrop AWDL spike (M0) starting");

	int err = radio_init(DEFAULT_CHANNEL);
	if (err != ESP_OK) {
		ESP_LOGE("espdrop", "radio_init failed: 0x%x", err);
		return;
	}

	esp_console_repl_t *repl = NULL;
	esp_console_repl_config_t repl_cfg = ESP_CONSOLE_REPL_CONFIG_DEFAULT();
	repl_cfg.prompt = "espdrop>";
	repl_cfg.max_cmdline_length = 256;

	esp_console_dev_usb_serial_jtag_config_t usb_cfg =
		ESP_CONSOLE_DEV_USB_SERIAL_JTAG_CONFIG_DEFAULT();
	ESP_ERROR_CHECK(esp_console_new_repl_usb_serial_jtag(&usb_cfg, &repl_cfg, &repl));

	esp_console_register_help_command();
	console_register_commands();

	ESP_ERROR_CHECK(esp_console_start_repl(repl));
	ESP_LOGI("espdrop", "console ready; type 'help'");
}
