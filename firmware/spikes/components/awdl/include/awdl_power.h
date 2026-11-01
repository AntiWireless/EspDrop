/*
 * EspDrop - when the AWDL radio may sleep (platform-independent policy).
 *
 * AWDL keeps the Wi-Fi radio receiving and sending all the time, which makes
 * the chip warm even when nobody uses it. In AUTO mode the radio only runs
 * while something needs it, and is off otherwise:
 *  - a host request: `power hold <secs>` (browsing, a receive window the user
 *    opened, a send), or the BLE wake for a send (`wake <secs>`);
 *  - an open TCP connection through the socket proxy, plus a short grace
 *    period after the last one, so a transfer is never cut by sleep.
 * Requests lapse when the host goes quiet (closed app, unplugged PC side)
 * for longer than AWDL_POWER_HOST_TIMEOUT_US. ON mode never sleeps (the L2
 * bridge and the console expect AWDL all the time).
 *
 * This file only decides; main/power.c switches the radio. Times are in
 * microseconds of a monotonic clock (esp_timer_get_time on the device).
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_AWDL_POWER_H
#define ESPDROP_AWDL_POWER_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define AWDL_POWER_BUSY_GRACE_US    (20LL * 1000000)   /* after the last TCP connection */
#define AWDL_POWER_HOST_TIMEOUT_US  (90LL * 1000000)   /* host silent: its requests lapse */

typedef enum {
	AWDL_POWER_AUTO = 0,   /* radio on only while needed */
	AWDL_POWER_ON   = 1,   /* radio always on */
} awdl_power_mode_t;

/* Why the radio is (or would be) on; for the `power` status line. */
typedef enum {
	AWDL_AWAKE_NONE = 0,   /* asleep */
	AWDL_AWAKE_ON,         /* ON mode */
	AWDL_AWAKE_HOLD,       /* power hold */
	AWDL_AWAKE_WAKE,       /* BLE wake for a send */
	AWDL_AWAKE_BUSY,       /* TCP connection (or its grace period) */
	AWDL_AWAKE_ADVERT,     /* an AirDrop advert was heard while listening */
} awdl_awake_reason_t;

#define AWDL_POWER_ADVERT_WAKE_US   (25LL * 1000000)   /* awake after an AirDrop advert */

typedef struct {
	awdl_power_mode_t mode;
	bool    listen;        /* watch for AirDrop adverts and wake on them */
	int64_t hold_until;    /* power hold deadline */
	int64_t wake_until;    /* BLE wake deadline */
	int64_t busy_until;    /* last TCP connection + grace */
	int64_t advert_until;  /* last AirDrop advert heard + wake */
	int64_t host_seen;     /* last message from the host */
} awdl_power_t;

void awdl_power_init(awdl_power_t *p, awdl_power_mode_t mode, int64_t now);
void awdl_power_set_mode(awdl_power_t *p, awdl_power_mode_t mode);

/* Keep the radio on for at least `secs` from now (0 releases the hold). */
void awdl_power_hold(awdl_power_t *p, int64_t now, uint32_t secs);
/* The BLE wake runs for `secs` from now (0: it was stopped). */
void awdl_power_wake(awdl_power_t *p, int64_t now, uint32_t secs);
/* Any message from the host. */
void awdl_power_host_seen(awdl_power_t *p, int64_t now);
/* Call regularly with the number of open TCP connections. */
void awdl_power_busy(awdl_power_t *p, int64_t now, int open_tcp);
/* Listen mode: watch for AirDrop adverts and wake the radio on them. */
void awdl_power_set_listen(awdl_power_t *p, bool on);
/* An AirDrop advert was heard: wake for a while (only acts while listening). */
void awdl_power_advert(awdl_power_t *p, int64_t now);
/* Should the BLE scan run now (listen mode, and the host is still around)? */
bool awdl_power_want_scan(const awdl_power_t *p, int64_t now);

/* Should the radio be on now, and why (AWDL_AWAKE_NONE: let it sleep). */
awdl_awake_reason_t awdl_power_reason(const awdl_power_t *p, int64_t now);

/* Seconds left on a deadline (0 when passed, or lapsed with the host). */
uint32_t awdl_power_left_s(const awdl_power_t *p, int64_t deadline, int64_t now);

const char *awdl_power_reason_name(awdl_awake_reason_t r);

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_AWDL_POWER_H */
