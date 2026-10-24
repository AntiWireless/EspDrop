/*
 * EspDrop - switch the AWDL radio off while nothing needs it. See power.h.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "power.h"

#include <stdio.h>

#include "esp_err.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "awdl_app.h"
#include "bridge.h"
#include "radio.h"
#include "sockproxy.h"

#define POWER_TICK_MS 500

static awdl_power_t      s_pol;
static portMUX_TYPE      s_mux = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t      s_task;
static volatile bool     s_awake = true;    /* the radio comes up awake at boot */
static uint32_t          s_sleeps, s_wakes, s_errors;
static int64_t           s_asleep_us, s_since;   /* time asleep, last switch */

static int64_t now_us(void) { return esp_timer_get_time(); }

static void kick(void) {
	if (s_task) xTaskNotifyGive(s_task);
}

/* Power task only: bring the radio to the wanted state. */
static void apply(awdl_awake_reason_t why) {
	bool want = why != AWDL_AWAKE_NONE;
	if (want == s_awake) return;
	int64_t t = now_us();
	if (want) {
		int err = radio_wake();
		awdl_app_sleep(false);
		s_awake = true;
		s_wakes++;
		s_asleep_us += t - s_since;
		if (err != ESP_OK) s_errors++;
		bridge_log("power: awake (%s)%s", awdl_power_reason_name(why),
		           err == ESP_OK ? "" : ", radio error");
	} else {
		awdl_app_sleep(true);       /* nothing is sent from here on */
		int err = radio_sleep();
		if (err != ESP_OK) {        /* stays on: try again next tick */
			awdl_app_sleep(false);
			s_errors++;
			return;
		}
		s_awake = false;
		s_sleeps++;
		bridge_log("power: asleep (Wi-Fi off)");
	}
	s_since = t;
}

static void power_task(void *arg) {
	(void)arg;
	for (;;) {
		(void)ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(POWER_TICK_MS));
		int open = sockproxy_tcp_open();
		int64_t t = now_us();
		portENTER_CRITICAL(&s_mux);
		awdl_power_busy(&s_pol, t, open);
		awdl_awake_reason_t why = awdl_power_reason(&s_pol, t);
		portEXIT_CRITICAL(&s_mux);
		apply(why);
	}
}

int power_init(awdl_power_mode_t mode) {
	if (s_task) return 0;
	s_since = now_us();
	awdl_power_init(&s_pol, mode, s_since);
	if (xTaskCreate(power_task, "power", 3072, NULL, 4, &s_task) != pdPASS) return -1;
	return 0;
}

void power_set_mode(awdl_power_mode_t mode) {
	portENTER_CRITICAL(&s_mux);
	awdl_power_set_mode(&s_pol, mode);
	portEXIT_CRITICAL(&s_mux);
	kick();
}

void power_hold(uint32_t secs) {
	int64_t t = now_us();
	portENTER_CRITICAL(&s_mux);
	awdl_power_hold(&s_pol, t, secs);
	awdl_power_host_seen(&s_pol, t);
	portEXIT_CRITICAL(&s_mux);
	kick();
}

void power_wake(uint32_t secs) {
	int64_t t = now_us();
	portENTER_CRITICAL(&s_mux);
	awdl_power_wake(&s_pol, t, secs);
	portEXIT_CRITICAL(&s_mux);
	kick();
}

void power_host_seen(void) {
	int64_t t = now_us();
	portENTER_CRITICAL(&s_mux);
	awdl_power_host_seen(&s_pol, t);
	portEXIT_CRITICAL(&s_mux);
}

bool power_awake(void) { return s_awake; }

int power_format_status(char *buf, size_t cap) {
	int64_t t = now_us();
	portENTER_CRITICAL(&s_mux);
	awdl_power_t p = s_pol;
	portEXIT_CRITICAL(&s_mux);
	awdl_awake_reason_t why = awdl_power_reason(&p, t);
	int64_t asleep = s_asleep_us + (s_awake ? 0 : t - s_since);
	int64_t up = t > 0 ? t : 1;
	return snprintf(buf, cap,
	                "power %s %s (%s) hold=%us wake=%us tcp=%d host=%us ago "
	                "sleeps=%u wakes=%u asleep=%u%% err=%u",
	                p.mode == AWDL_POWER_ON ? "on" : "auto",
	                s_awake ? "awake" : "asleep", awdl_power_reason_name(why),
	                (unsigned)awdl_power_left_s(&p, p.hold_until, t),
	                (unsigned)awdl_power_left_s(&p, p.wake_until, t),
	                sockproxy_tcp_open(), (unsigned)((t - p.host_seen) / 1000000),
	                (unsigned)s_sleeps, (unsigned)s_wakes,
	                (unsigned)(asleep * 100 / up), (unsigned)s_errors);
}
