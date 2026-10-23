/*
 * EspDrop - AirDrop wake over the chip's own Bluetooth LE. See ble_wake.h.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "ble_wake.h"

#include <string.h>

#include "esp_err.h"
#include "esp_timer.h"
#include "sdkconfig.h"

#if CONFIG_BT_NIMBLE_ENABLED

#include "host/ble_hs.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"

/* 200-250 ms: often enough that a phone scanning with its share sheet open sees
 * the advert within a second, rare enough that BLE costs AWDL little: it shares
 * the radio with Wi-Fi and, on the C5 parked on 5 GHz, needs a band switch for
 * every advertising event. */
#define WAKE_ITVL_MIN BLE_GAP_ADV_ITVL_MS(200)
#define WAKE_ITVL_MAX BLE_GAP_ADV_ITVL_MS(250)
#define WAKE_ADV_LEN  24

static volatile bool    s_ready;    /* host synced with the controller */
static volatile bool    s_active;   /* our advert is running */
static volatile int64_t s_end_us;   /* when it stops by itself */

static void on_sync(void) { s_ready = true; }

static void on_reset(int reason) {
	(void)reason;
	s_ready = false;
	s_active = false;
}

static void host_task(void *arg) {
	(void)arg;
	nimble_port_run();              /* returns only after nimble_port_stop() */
	nimble_port_freertos_deinit();
}

/* Host task context. The advert ends by itself at its duration. */
static int on_gap(struct ble_gap_event *ev, void *arg) {
	(void)arg;
	if (ev->type == BLE_GAP_EVENT_ADV_COMPLETE) s_active = false;
	return 0;
}

int ble_wake_init(void) {
	esp_err_t err = nimble_port_init();   /* controller + host */
	if (err != ESP_OK) return err;
	ble_hs_cfg.sync_cb = on_sync;
	ble_hs_cfg.reset_cb = on_reset;
	nimble_port_freertos_init(host_task);
	return ESP_OK;
}

bool ble_wake_ready(void) { return s_ready; }

/* One AD structure, manufacturer specific data: Apple (0x004C), Continuity
 * type 0x05 "AirDrop" - byte for byte what tools/airdrop_wake.py advertises:
 *   05 12  <8 zero bytes>  01  <4 x 2-byte hash>  00 */
static int build_adv(uint8_t out[WAKE_ADV_LEN], const uint8_t hashes[8]) {
	static const uint8_t zero[8];
	if (!hashes) hashes = zero;
	int o = 0;
	out[o++] = WAKE_ADV_LEN - 1;          /* AD length: everything after this byte */
	out[o++] = 0xff;                      /* manufacturer specific data */
	out[o++] = 0x4c;                      /* company id 0x004C, little endian */
	out[o++] = 0x00;
	out[o++] = 0x05;                      /* Continuity type: AirDrop */
	out[o++] = 0x12;                      /* 18 bytes follow */
	memset(out + o, 0, 8);                /* zero prefix */
	o += 8;
	out[o++] = 0x01;                      /* AirDrop version */
	memcpy(out + o, hashes, 8);           /* Apple ID, phone, email, email2 */
	o += 8;
	out[o++] = 0x00;
	return o;
}

int ble_wake_start(unsigned secs, const uint8_t hashes[8]) {
	if (!s_ready) return BLE_HS_ENOTSYNCED;
	if (secs < 1 || secs > BLE_WAKE_MAX_SECS) return BLE_HS_EINVAL;

	int rc;
	if (ble_gap_adv_active()) {
		/* Called from the bridge task, below the NimBLE host's priority, so a
		 * stop followed straight by a start is safe (see ble_gap_adv_stop). */
		rc = ble_gap_adv_stop();
		if (rc != 0 && rc != BLE_HS_EALREADY) return rc;
	} else {
		/* A fresh non-resolvable private address per wake, as Apple devices
		 * rotate theirs; it can only change while not advertising. */
		ble_addr_t a;
		rc = ble_hs_id_gen_rnd(1, &a);
		if (rc == 0) rc = ble_hs_id_set_rnd(a.val);
		if (rc != 0) return rc;
	}

	uint8_t adv[WAKE_ADV_LEN];
	rc = ble_gap_adv_set_data(adv, build_adv(adv, hashes));
	if (rc != 0) return rc;

	struct ble_gap_adv_params p = {0};
	p.conn_mode = BLE_GAP_CONN_MODE_NON;  /* broadcast only: ADV_NONCONN_IND */
	p.disc_mode = BLE_GAP_DISC_MODE_NON;
	p.itvl_min = WAKE_ITVL_MIN;
	p.itvl_max = WAKE_ITVL_MAX;
	rc = ble_gap_adv_start(BLE_OWN_ADDR_RANDOM, NULL, (int32_t)secs * 1000, &p,
	                       on_gap, NULL);
	if (rc != 0) return rc;
	s_end_us = esp_timer_get_time() + (int64_t)secs * 1000000;
	s_active = true;
	return 0;
}

int ble_wake_stop(void) {
	if (!s_ready || !ble_gap_adv_active()) {
		s_active = false;
		return 0;
	}
	int rc = ble_gap_adv_stop();
	s_active = false;
	return rc == BLE_HS_EALREADY ? 0 : rc;
}

unsigned ble_wake_remaining(void) {
	if (!s_active) return 0;
	int64_t left = s_end_us - esp_timer_get_time();
	return left > 0 ? (unsigned)((left + 999999) / 1000000) : 0;
}

#else /* !CONFIG_BT_NIMBLE_ENABLED: firmware built without Bluetooth */

int ble_wake_init(void) { return ESP_ERR_NOT_SUPPORTED; }
bool ble_wake_ready(void) { return false; }
int ble_wake_start(unsigned secs, const uint8_t hashes[8]) {
	(void)secs;
	(void)hashes;
	return ESP_ERR_NOT_SUPPORTED;
}
int ble_wake_stop(void) { return 0; }
unsigned ble_wake_remaining(void) { return 0; }

#endif
