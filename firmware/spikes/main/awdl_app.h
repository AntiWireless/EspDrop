/*
 * EspDrop - AWDL application layer (ESP32).
 *
 * Owns the platform-independent awdl_node brain and drives it from one FreeRTOS
 * task: received action frames arrive via a queue (fed by the radio handler),
 * and PSF/MIF transmission plus peer cleanup are time-driven off
 * esp_timer_get_time(). The node is only ever touched under one mutex, so the
 * console can read a consistent snapshot.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_AWDL_APP_H
#define ESPDROP_AWDL_APP_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "awdl_node.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Start the AWDL task on the given self MAC, name and 2.4 GHz channel. Registers
 * the radio action handler. Call once, after radio_init(). Returns 0 on success.
 */
int awdl_app_start(const uint8_t self_mac[6], const char *name, uint8_t chan);

/* Rebuild the node for a new self MAC / channel (resets sync). For the console
 * mac/chan commands. Safe to call at runtime. */
void awdl_app_set_self(const uint8_t self_mac[6], uint8_t chan);

/* Copy a consistent status snapshot out. Returns false if not started. */
bool awdl_app_status(awdl_node_status_t *out);

/* Format the current peer list into buf (NUL-terminated). Returns bytes used. */
int awdl_app_format_peers(char *buf, size_t cap);

/* Build and transmit one of our own PSF/MIF frames immediately (console `tx`). */
void awdl_app_tx(uint8_t subtype);

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_AWDL_APP_H */
