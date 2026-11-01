/*
 * EspDrop - when the AWDL radio may sleep. See awdl_power.h.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_power.h"

void awdl_power_init(awdl_power_t *p, awdl_power_mode_t mode, int64_t now) {
	p->mode = mode;
	p->listen = false;
	p->hold_until = p->wake_until = p->busy_until = p->advert_until = now;
	p->host_seen = now;
}

void awdl_power_set_mode(awdl_power_t *p, awdl_power_mode_t mode) { p->mode = mode; }

void awdl_power_hold(awdl_power_t *p, int64_t now, uint32_t secs) {
	p->hold_until = now + (int64_t)secs * 1000000;
}

void awdl_power_wake(awdl_power_t *p, int64_t now, uint32_t secs) {
	p->wake_until = now + (int64_t)secs * 1000000;
}

void awdl_power_host_seen(awdl_power_t *p, int64_t now) { p->host_seen = now; }

void awdl_power_busy(awdl_power_t *p, int64_t now, int open_tcp) {
	if (open_tcp > 0 && now + AWDL_POWER_BUSY_GRACE_US > p->busy_until)
		p->busy_until = now + AWDL_POWER_BUSY_GRACE_US;
}

void awdl_power_set_listen(awdl_power_t *p, bool on) { p->listen = on; }

void awdl_power_advert(awdl_power_t *p, int64_t now) {
	if (now + AWDL_POWER_ADVERT_WAKE_US > p->advert_until)
		p->advert_until = now + AWDL_POWER_ADVERT_WAKE_US;
}

/* A host request counts while the host is around: a deadline beyond the host
 * timeout is cut back to it, so a closed app does not keep the radio on. */
static int64_t effective(const awdl_power_t *p, int64_t deadline) {
	int64_t cap = p->host_seen + AWDL_POWER_HOST_TIMEOUT_US;
	return deadline < cap ? deadline : cap;
}

awdl_awake_reason_t awdl_power_reason(const awdl_power_t *p, int64_t now) {
	if (p->mode == AWDL_POWER_ON) return AWDL_AWAKE_ON;
	if (now < p->busy_until) return AWDL_AWAKE_BUSY;
	if (now < effective(p, p->hold_until)) return AWDL_AWAKE_HOLD;
	if (now < effective(p, p->wake_until)) return AWDL_AWAKE_WAKE;
	if (p->listen && now < effective(p, p->advert_until)) return AWDL_AWAKE_ADVERT;
	return AWDL_AWAKE_NONE;
}

/* The scan runs while the host wants to listen and is still around (a host gone
 * quiet for the timeout cannot receive, so there is nothing to wake for). */
bool awdl_power_want_scan(const awdl_power_t *p, int64_t now) {
	return p->listen && now < p->host_seen + AWDL_POWER_HOST_TIMEOUT_US;
}

uint32_t awdl_power_left_s(const awdl_power_t *p, int64_t deadline, int64_t now) {
	int64_t d = effective(p, deadline) - now;
	return d > 0 ? (uint32_t)((d + 999999) / 1000000) : 0;
}

const char *awdl_power_reason_name(awdl_awake_reason_t r) {
	switch (r) {
	case AWDL_AWAKE_ON:   return "on";
	case AWDL_AWAKE_HOLD: return "hold";
	case AWDL_AWAKE_WAKE: return "wake";
	case AWDL_AWAKE_BUSY: return "busy";
	case AWDL_AWAKE_ADVERT: return "advert";
	default:              return "none";
	}
}
