/*
 * EspDrop - AirDrop wake over the chip's own Bluetooth LE.
 *
 * To be found as a SENDER's target, an iPhone/iPad must hear a Continuity
 * "AirDrop" advertisement (Apple manufacturer data, type 0x05) while its share
 * sheet is open; only then does it answer for _airdrop._tcp over AWDL
 * (airdrop-mt7921 FINDINGS §27). This sends that advert from the ESP itself,
 * the same bytes as tools/airdrop_wake.py sends from the PC's Bluetooth.
 *
 * The advert is always timed: it stops by itself after the requested time, so
 * a host script that dies cannot leave nearby phones being woken forever.
 *
 * Built only with CONFIG_BT_NIMBLE_ENABLED (bridge firmware); otherwise every
 * call reports Bluetooth as unavailable.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define BLE_WAKE_MAX_SECS 600   /* longest single wake, like the PC tool's default */

/* Bring up the BLE controller and NimBLE host. Call once at boot, after the
 * Wi-Fi radio is up (coexistence) and before the bridge takes the USB port
 * (the controller logs at init). Returns an esp_err_t. */
int ble_wake_init(void);

/* True once the BLE host has synced with the controller and can advertise. */
bool ble_wake_ready(void);

/*
 * Advertise the AirDrop wake for secs seconds (1..BLE_WAKE_MAX_SECS). hashes:
 * the four 2-byte SHA-256 prefixes (Apple ID, phone, email, email2) in that
 * order, or NULL for all zero (target on "Everyone"). Calling it while a wake
 * runs restarts it with the new time and hashes, keeping the BLE address; a
 * wake started from off gets a fresh random address. Returns 0 or a NimBLE /
 * esp_err_t code.
 */
int ble_wake_start(unsigned secs, const uint8_t hashes[8]);

/* Stop the wake now (no-op if it is off). Returns 0 or an error code. */
int ble_wake_stop(void);

/* Seconds the running wake has left, 0 if it is off. */
unsigned ble_wake_remaining(void);

/*
 * Listen for AirDrop over Bluetooth, the other way round: an iPhone/iPad with
 * its share sheet open sends the same Continuity "AirDrop" advert (type 0x05)
 * to find receivers. A passive BLE scan watches for it, so main/power.c can
 * keep the Wi-Fi radio asleep and only wake AWDL when a phone nearby actually
 * wants to share, instead of keeping AWDL on all the time. cb runs on the
 * NimBLE host task for every AirDrop advert seen (no device identity is kept;
 * only "an AirDrop advert is in range" matters). Needs the observer role in
 * the build; ble_scan_supported() is false otherwise and ble_scan_start fails.
 */
typedef void (*ble_scan_cb_t)(void *ctx);
bool ble_scan_supported(void);
int ble_scan_start(ble_scan_cb_t cb, void *ctx);
int ble_scan_stop(void);
bool ble_scan_active(void);

#ifdef __cplusplus
}
#endif
