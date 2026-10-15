/*
 * EspDrop - native host unit test for the COBS codec and stream reassembler.
 *
 * Build & run:  make -C firmware/spikes/components/awdl/test/host run
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_cobs.h"

#include <stdio.h>
#include <string.h>

static int g_fail;

#define CHECK(cond, ...) do { \
	if (!(cond)) { printf("FAIL %s:%d: ", __func__, __LINE__); printf(__VA_ARGS__); printf("\n"); g_fail++; } \
} while (0)

/* encode then decode must reproduce the input exactly */
static void roundtrip(const uint8_t *in, size_t len, const char *label) {
	uint8_t enc[COBS_ENCODE_BOUND(2048)];
	uint8_t dec[2048];
	size_t e = cobs_encode(in, len, enc, sizeof(enc));
	CHECK(e > 0 || len == 0, "%s: encode returned 0 for len %zu", label, len);
	/* the encoded form must contain no zero byte */
	for (size_t i = 0; i < e; i++)
		CHECK(enc[i] != 0, "%s: encoded byte %zu is zero", label, i);
	size_t d = cobs_decode(enc, e, dec, sizeof(dec));
	CHECK(d == len, "%s: decoded len %zu != %zu", label, d, len);
	CHECK(memcmp(dec, in, len) == 0, "%s: decoded payload differs", label);
}

static void test_vectors(void) {
	uint8_t a[] = {0x11, 0x22, 0x00, 0x33};
	roundtrip(a, sizeof(a), "mixed");

	uint8_t z1[] = {0x00};
	roundtrip(z1, sizeof(z1), "single-zero");

	uint8_t z3[] = {0x00, 0x00, 0x00};
	roundtrip(z3, sizeof(z3), "zeros");

	uint8_t one[] = {0x42};
	roundtrip(one, sizeof(one), "one");

	/* a 254-byte non-zero run (exercises the 0xFF block boundary) and 255 */
	uint8_t r254[254], r255[255], r300[300];
	for (int i = 0; i < 254; i++) r254[i] = (uint8_t)(i + 1);
	for (int i = 0; i < 255; i++) r255[i] = (uint8_t)((i % 255) + 1);
	for (int i = 0; i < 300; i++) r300[i] = (uint8_t)((i % 255) + 1);
	roundtrip(r254, sizeof(r254), "run254");
	roundtrip(r255, sizeof(r255), "run255");
	roundtrip(r300, sizeof(r300), "run300");

	/* zeros embedded around long runs */
	uint8_t mix[600];
	for (int i = 0; i < 600; i++) mix[i] = (i % 7 == 0) ? 0 : (uint8_t)((i & 0xff) | 1);
	roundtrip(mix, sizeof(mix), "mix600");
}

/* deterministic pseudo-random fuzz: encode->decode identity over many buffers */
static void test_fuzz(void) {
	uint32_t s = 0x12345678u;
	for (int iter = 0; iter < 20000; iter++) {
		uint8_t in[400];
		s = s * 1103515245u + 12345u;
		size_t len = (s >> 16) % (sizeof(in) + 1);
		for (size_t i = 0; i < len; i++) {
			s = s * 1103515245u + 12345u;
			/* bias toward zeros so block boundaries get exercised */
			in[i] = ((s >> 20) % 4 == 0) ? 0 : (uint8_t)(s >> 8);
		}
		uint8_t enc[COBS_ENCODE_BOUND(sizeof(in))];
		uint8_t dec[sizeof(in)];
		size_t e = cobs_encode(in, len, enc, sizeof(enc));
		for (size_t i = 0; i < e; i++)
			if (enc[i] == 0) { CHECK(0, "fuzz iter %d: zero in encoded", iter); return; }
		size_t d = cobs_decode(enc, e, dec, sizeof(dec));
		if (d != len || memcmp(dec, in, len) != 0) {
			CHECK(0, "fuzz iter %d: roundtrip mismatch (len %zu, d %zu)", iter, len, d);
			return;
		}
	}
}

static void test_encode_overflow(void) {
	uint8_t in[300];
	memset(in, 0x55, sizeof(in));
	uint8_t small[8];
	CHECK(cobs_encode(in, sizeof(in), small, sizeof(small)) == 0,
	      "encode into too-small buffer should return 0");
}

static void test_decode_bad(void) {
	/* a zero byte inside the frame body is malformed */
	uint8_t bad[] = {0x03, 0x11, 0x00};
	uint8_t out[16];
	CHECK(cobs_decode(bad, sizeof(bad), out, sizeof(out)) == 0,
	      "decode of frame containing 0x00 should fail");
	/* a code that points past the end is truncated */
	uint8_t trunc[] = {0x05, 0x11, 0x22};
	CHECK(cobs_decode(trunc, sizeof(trunc), out, sizeof(out)) == 0,
	      "decode of truncated block should fail");
}

/* feed an encoded frame + delimiter through the stream reassembler */
static void test_stream(void) {
	cobs_rx_t rx;
	cobs_rx_init(&rx);

	uint8_t msg[] = {0x07, 'h', 'i', 0x00, 'x'}; /* includes a zero */
	uint8_t enc[COBS_ENCODE_BOUND(sizeof(msg))];
	size_t e = cobs_encode(msg, sizeof(msg), enc, sizeof(enc));

	uint8_t out[64];
	/* leading stray delimiter must be ignored */
	CHECK(cobs_rx_byte(&rx, 0x00, out, sizeof(out)) == 0, "leading delim not ignored");
	int r = 0;
	for (size_t i = 0; i < e; i++)
		CHECK(cobs_rx_byte(&rx, enc[i], out, sizeof(out)) == 0, "mid-frame returned a frame");
	r = cobs_rx_byte(&rx, 0x00, out, sizeof(out)); /* delimiter completes it */
	CHECK(r == (int)sizeof(msg), "stream frame len %d != %zu", r, sizeof(msg));
	CHECK(memcmp(out, msg, sizeof(msg)) == 0, "stream payload differs");

	/* a second, back-to-back frame */
	uint8_t msg2[] = {0x04, 0xAA, 0xBB};
	e = cobs_encode(msg2, sizeof(msg2), enc, sizeof(enc));
	for (size_t i = 0; i < e; i++) cobs_rx_byte(&rx, enc[i], out, sizeof(out));
	r = cobs_rx_byte(&rx, 0x00, out, sizeof(out));
	CHECK(r == (int)sizeof(msg2) && memcmp(out, msg2, sizeof(msg2)) == 0, "second frame");

	/* overflow: more non-zero bytes than the buffer holds -> -1 at delimiter */
	cobs_rx_init(&rx);
	for (size_t i = 0; i < COBS_RX_CAP + 10; i++) cobs_rx_byte(&rx, 0x42, out, sizeof(out));
	CHECK(cobs_rx_byte(&rx, 0x00, out, sizeof(out)) == -1, "overflow not reported");
	/* reassembler recovers for the next frame */
	e = cobs_encode(msg2, sizeof(msg2), enc, sizeof(enc));
	for (size_t i = 0; i < e; i++) cobs_rx_byte(&rx, enc[i], out, sizeof(out));
	CHECK(cobs_rx_byte(&rx, 0x00, out, sizeof(out)) == (int)sizeof(msg2),
	      "no recovery after overflow");
}

int main(void) {
	test_vectors();
	test_fuzz();
	test_encode_overflow();
	test_decode_bad();
	test_stream();

	if (g_fail == 0) printf("COBS: ALL TESTS PASSED\n");
	else printf("COBS: %d CHECK(S) FAILED\n", g_fail);
	return g_fail ? 1 : 0;
}
