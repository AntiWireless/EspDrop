/*
 * EspDrop - AWDL synchronization math (platform independent).
 *
 * See awdl_sync.h. Ported from OWL src/sync.c (GPL-3.0).
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_sync.h"

static inline uint64_t tu_to_us(uint64_t tu) { return tu * AWDL_TU_US; }
static inline uint64_t us_to_tu(uint64_t us) { return us / AWDL_TU_US; }

void awdl_sync_init(awdl_sync_t *s, uint64_t now_us) {
	s->last_update_us = now_us;
	s->aw_counter = 0;
	s->aw_period_tu = AWDL_AW_PERIOD_TU;
	s->presence_mode = AWDL_PRESENCE_MODE;
}

uint16_t awdl_sync_next_aw_tu(uint64_t now_us, const awdl_sync_t *s) {
	uint64_t eaw_period = (uint64_t)s->presence_mode * s->aw_period_tu;
	uint64_t since = us_to_tu(now_us - s->last_update_us);
	return (uint16_t)(eaw_period - (since % eaw_period));
}

uint16_t awdl_sync_current_aw(uint64_t now_us, const awdl_sync_t *s) {
	uint64_t eaw_period = (uint64_t)s->presence_mode * s->aw_period_tu;
	uint64_t since = us_to_tu(now_us - s->last_update_us);
	uint64_t aw = (uint64_t)s->aw_counter +
	              (since % eaw_period) / s->aw_period_tu +
	              (uint64_t)s->presence_mode * (since / eaw_period);
	return (uint16_t)aw;
}

uint16_t awdl_sync_current_eaw(uint64_t now_us, const awdl_sync_t *s) {
	return (uint16_t)(awdl_sync_current_aw(now_us, s) / s->presence_mode);
}

int64_t awdl_sync_error_tu(uint64_t now_us, uint16_t time_to_next_aw,
                           uint16_t aw_counter, const awdl_sync_t *s) {
	int64_t eaw_diff = (int64_t)(aw_counter / s->presence_mode) -
	                   (int64_t)awdl_sync_current_eaw(now_us, s);
	int64_t aw_term = eaw_diff * (int64_t)s->presence_mode * (int64_t)s->aw_period_tu;
	int64_t phase = (int64_t)time_to_next_aw - (int64_t)awdl_sync_next_aw_tu(now_us, s);
	return aw_term - phase;
}

void awdl_sync_update(uint64_t now_us, uint16_t time_to_next_aw,
                      uint16_t aw_counter, awdl_sync_t *s) {
	uint64_t eaw_period = (uint64_t)s->presence_mode * s->aw_period_tu;
	s->last_update_us = now_us - tu_to_us(eaw_period - time_to_next_aw);
	s->aw_counter = aw_counter & 0xfffc; /* round down to EAW boundary (presence_mode=4) */
}
