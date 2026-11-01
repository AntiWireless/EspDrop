/*
 * Host tests for awdl_power: when the AWDL radio may sleep.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#include <stdio.h>
#include <stdlib.h>

#include "awdl_power.h"

#define S(x) ((int64_t)(x) * 1000000)

static int failures;
#define CHECK(cond) do { \
	if (!(cond)) { printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); failures++; } \
} while (0)

static void test_auto_sleeps_without_requests(void) {
	awdl_power_t p;
	awdl_power_init(&p, AWDL_POWER_AUTO, S(100));
	CHECK(awdl_power_reason(&p, S(100)) == AWDL_AWAKE_NONE);
	awdl_power_set_mode(&p, AWDL_POWER_ON);
	CHECK(awdl_power_reason(&p, S(5000)) == AWDL_AWAKE_ON);
}

static void test_hold_and_release(void) {
	awdl_power_t p;
	awdl_power_init(&p, AWDL_POWER_AUTO, 0);
	awdl_power_hold(&p, S(10), 60);
	awdl_power_host_seen(&p, S(10));
	CHECK(awdl_power_reason(&p, S(69)) == AWDL_AWAKE_HOLD);
	CHECK(awdl_power_left_s(&p, p.hold_until, S(40)) == 30);
	CHECK(awdl_power_reason(&p, S(70)) == AWDL_AWAKE_NONE);
	awdl_power_hold(&p, S(80), 60);
	awdl_power_hold(&p, S(81), 0);                      /* release */
	CHECK(awdl_power_reason(&p, S(82)) == AWDL_AWAKE_NONE);
}

static void test_wake_and_busy(void) {
	awdl_power_t p;
	awdl_power_init(&p, AWDL_POWER_AUTO, 0);
	awdl_power_wake(&p, S(1), 30);
	CHECK(awdl_power_reason(&p, S(20)) == AWDL_AWAKE_WAKE);
	awdl_power_busy(&p, S(25), 1);                      /* a transfer is running */
	CHECK(awdl_power_reason(&p, S(40)) == AWDL_AWAKE_BUSY);
	awdl_power_busy(&p, S(40), 0);                      /* it ended at ~25 s */
	CHECK(awdl_power_reason(&p, S(44)) == AWDL_AWAKE_BUSY);   /* grace: 25 + 20 */
	CHECK(awdl_power_reason(&p, S(45)) == AWDL_AWAKE_NONE);
}

static void test_requests_lapse_with_the_host(void) {
	awdl_power_t p;
	awdl_power_init(&p, AWDL_POWER_AUTO, 0);
	awdl_power_host_seen(&p, S(10));
	awdl_power_hold(&p, S(10), 600);                    /* a long receive window */
	CHECK(awdl_power_reason(&p, S(99)) == AWDL_AWAKE_HOLD);
	CHECK(awdl_power_reason(&p, S(100)) == AWDL_AWAKE_NONE);  /* host silent 90 s */
	awdl_power_host_seen(&p, S(120));                   /* it is back: hold counts again */
	CHECK(awdl_power_reason(&p, S(121)) == AWDL_AWAKE_HOLD);
	/* An open connection keeps the radio on even when the host is quiet. */
	awdl_power_busy(&p, S(300), 2);
	CHECK(awdl_power_reason(&p, S(310)) == AWDL_AWAKE_BUSY);
}

static void test_listen_and_advert(void) {
	awdl_power_t p;
	awdl_power_init(&p, AWDL_POWER_AUTO, 0);
	awdl_power_host_seen(&p, S(10));
	/* Listen off: no scan, and an advert does not wake the radio. */
	CHECK(!awdl_power_want_scan(&p, S(11)));
	awdl_power_advert(&p, S(11));
	CHECK(awdl_power_reason(&p, S(12)) == AWDL_AWAKE_NONE);
	/* Listen on: the scan runs; that earlier advert does not linger past its window. */
	awdl_power_set_listen(&p, true);
	CHECK(awdl_power_want_scan(&p, S(12)));
	CHECK(awdl_power_reason(&p, S(40)) == AWDL_AWAKE_NONE);   /* 11 + 25 s passed */
	/* A fresh advert wakes it, and a later one re-arms it (share sheet still open). */
	awdl_power_advert(&p, S(40));
	CHECK(awdl_power_reason(&p, S(50)) == AWDL_AWAKE_ADVERT);
	CHECK(awdl_power_reason(&p, S(66)) == AWDL_AWAKE_NONE);   /* 40 + 25 s passed */
	awdl_power_advert(&p, S(60));
	CHECK(awdl_power_reason(&p, S(80)) == AWDL_AWAKE_ADVERT);
	/* Host goes quiet: no scan, and adverts stop keeping it awake. */
	CHECK(awdl_power_want_scan(&p, S(10 + 89)));
	CHECK(!awdl_power_want_scan(&p, S(10 + 90)));
	awdl_power_advert(&p, S(200));
	CHECK(awdl_power_reason(&p, S(205)) == AWDL_AWAKE_NONE);  /* host silent since 10 s */
}

int main(void) {
	test_auto_sleeps_without_requests();
	test_hold_and_release();
	test_wake_and_busy();
	test_requests_lapse_with_the_host();
	test_listen_and_advert();
	if (failures) {
		printf("test_awdl_power: %d failure(s)\n", failures);
		return EXIT_FAILURE;
	}
	printf("test_awdl_power: all passed\n");
	return EXIT_SUCCESS;
}
