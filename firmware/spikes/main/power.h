/*
 * EspDrop - switch the AWDL radio off while nothing needs it.
 *
 * The policy (when the radio may sleep) is components/awdl/awdl_power.h; this
 * runs it on a small task and switches the radio: asleep means the AWDL task
 * sends nothing (awdl_app_sleep) and Wi-Fi is stopped (radio_sleep). Bluetooth
 * is untouched, so the BLE wake for a send keeps running either way.
 *
 * The bridge feeds it: every host message (power_host_seen), `power hold`,
 * `wake <secs>`, and the mode from HELLO (L2: always on, IP: auto).
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "awdl_power.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Start the task in `mode`. Call once, after awdl_app_start and bridge_start. */
int power_init(awdl_power_mode_t mode);

void power_set_mode(awdl_power_mode_t mode);
void power_hold(uint32_t secs);          /* 0 releases */
void power_wake(uint32_t secs);          /* BLE wake running for secs; 0 stopped */
void power_set_listen(bool on);          /* watch for AirDrop adverts and wake on them */
bool power_listen_supported(void);       /* false without the BLE observer role */
void power_host_seen(void);
bool power_awake(void);

/* One-line status for the `power` command. */
int power_format_status(char *buf, size_t cap);

#ifdef __cplusplus
}
#endif
