/*
 * EspDrop - COBS framing (platform independent). See awdl_cobs.h.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_cobs.h"

size_t cobs_encode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap) {
	if (out_cap == 0) return 0;
	size_t out_i = 0;
	size_t code_i = out_i++; /* reserve the first code byte */
	uint8_t code = 1;

	for (size_t i = 0; i < len; i++) {
		if (in[i] == 0) {
			out[code_i] = code;       /* close the current block */
			if (out_i >= out_cap) return 0;
			code_i = out_i++;         /* reserve the next code byte */
			code = 1;
		} else {
			if (out_i >= out_cap) return 0;
			out[out_i++] = in[i];
			code++;
			if (code == 0xFF) {       /* maximum-length block: close it */
				out[code_i] = code;
				if (out_i >= out_cap) return 0;
				code_i = out_i++;     /* reserve the next code byte */
				code = 1;
			}
		}
	}
	out[code_i] = code;               /* close the final block */
	return out_i;
}

size_t cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap) {
	size_t in_i = 0, out_i = 0;
	while (in_i < len) {
		uint8_t code = in[in_i++];
		if (code == 0) return 0;      /* a 0x00 must not appear inside a frame */
		for (uint8_t j = 1; j < code; j++) {
			if (in_i >= len) return 0;      /* truncated block */
			if (out_i >= out_cap) return 0; /* output overflow */
			uint8_t v = in[in_i++];
			if (v == 0) return 0;           /* a well-formed frame has no 0x00 */
			out[out_i++] = v;
		}
		/* Every block but a maximal (0xFF) one implies a trailing zero, except
		 * the very last block (whose trailing zero was the stripped delimiter). */
		if (code != 0xFF && in_i < len) {
			if (out_i >= out_cap) return 0;
			out[out_i++] = 0;
		}
	}
	return out_i;
}

void cobs_rx_init(cobs_rx_t *rx) {
	rx->len = 0;
	rx->overflow = false;
}

int cobs_rx_byte(cobs_rx_t *rx, uint8_t b, uint8_t *out, size_t out_cap) {
	if (b != 0x00) {
		if (rx->len < sizeof(rx->buf))
			rx->buf[rx->len++] = b;
		else
			rx->overflow = true; /* frame too long; drop until the next delimiter */
		return 0;
	}

	/* delimiter: the current frame ends here */
	if (rx->overflow) {              /* frame was too long */
		cobs_rx_init(rx);
		return -1;
	}
	if (rx->len == 0)                /* empty frame (leading/duplicate delimiter) */
		return 0;                    /* ignore silently */

	size_t n = cobs_decode(rx->buf, rx->len, out, out_cap);
	cobs_rx_init(rx);
	return (n == 0) ? -1 : (int)n;
}
