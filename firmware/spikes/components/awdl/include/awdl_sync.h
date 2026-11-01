/*
 * EspDrop - AWDL synchronization math (platform independent).
 *
 * Ported field-for-field from OWL src/sync.{c,h} (The Open Wireless Link
 * Project / Milan Stute, GPL-3.0). AWDL time is counted in availability windows
 * (AW, 16 TU) grouped into extended availability windows (EAW = presence_mode
 * AWs). One TU is 1024 microseconds.
 *
 * All times are microseconds on a single monotonic clock (on the ESP32 that is
 * esp_timer_get_time()); there is no wall-clock dependency, so this is testable
 * on a host with made-up timestamps.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_AWDL_SYNC_H
#define ESPDROP_AWDL_SYNC_H

#include <stdint.h>

#include "awdl_frame.h" /* AWDL_TU_US, AWDL_AW_PERIOD_TU, AWDL_PRESENCE_MODE */

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
	uint64_t last_update_us; /* our estimate of when AW counter `aw_counter` began */
	uint16_t aw_counter;     /* AW index at last_update_us */
	uint16_t aw_period_tu;   /* AW length in TU (16) */
	uint8_t  presence_mode;  /* AWs per EAW (4) */
} awdl_sync_t;

void awdl_sync_init(awdl_sync_t *s, uint64_t now_us);

/* TU until the next AW boundary. */
uint16_t awdl_sync_next_aw_tu(uint64_t now_us, const awdl_sync_t *s);

/* Current AW index and EAW (slot) index. */
uint16_t awdl_sync_current_aw(uint64_t now_us, const awdl_sync_t *s);
uint16_t awdl_sync_current_eaw(uint64_t now_us, const awdl_sync_t *s);

/*
 * Phase error in TU between a master's announced timing (time_to_next_aw and
 * aw_counter, as carried in its synchronization-parameters TLV) and ours.
 * Zero means we are in sync; the sign tells which way we are off.
 */
int64_t awdl_sync_error_tu(uint64_t now_us, uint16_t time_to_next_aw,
                           uint16_t aw_counter, const awdl_sync_t *s);

/* Adopt a master's announced timing as our own AW clock. */
void awdl_sync_update(uint64_t now_us, uint16_t time_to_next_aw,
                      uint16_t aw_counter, awdl_sync_t *s);

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_AWDL_SYNC_H */
