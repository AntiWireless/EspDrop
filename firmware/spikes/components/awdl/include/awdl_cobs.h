/*
 * EspDrop - COBS framing for the USB host link (platform independent).
 *
 * Consistent Overhead Byte Stuffing: encode a message so it contains no 0x00
 * byte, then a single 0x00 delimiter marks the end of a frame on the wire.
 * This gives us unambiguous message boundaries over the USB-Serial/JTAG stream,
 * which carries binary Ethernet frames and control messages (see the M2 plan).
 *
 * Pure C, no ESP-IDF: encode/decode and a byte-at-a-time stream reassembler,
 * all host-testable.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_AWDL_COBS_H
#define ESPDROP_AWDL_COBS_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Worst-case COBS overhead is one byte per 254 input bytes, plus a leading
 * code byte. A caller sizing an output buffer can use this bound. */
#define COBS_ENCODE_BOUND(n) ((n) + (n) / 254 + 2)

/*
 * Encode `len` bytes from `in` into `out` (capacity `out_cap`). The result
 * contains no 0x00 byte; the caller appends a single 0x00 delimiter on the
 * wire. Returns the encoded length, or 0 on output overflow.
 */
size_t cobs_encode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap);

/*
 * Decode `len` COBS bytes from `in` (the frame contents, WITHOUT the trailing
 * 0x00 delimiter) into `out` (capacity `out_cap`). Returns the decoded length,
 * or 0 on a malformed frame or output overflow. (A genuinely empty decode also
 * returns 0; callers never frame empty messages, so 0 means "drop".)
 */
size_t cobs_decode(const uint8_t *in, size_t len, uint8_t *out, size_t out_cap);

/* ---- byte-at-a-time stream reassembler ------------------------------------
 * Feed received bytes one at a time; a complete frame is signalled when the
 * 0x00 delimiter arrives.
 */
#ifndef COBS_RX_CAP
#define COBS_RX_CAP 1664 /* enough for a 1514-byte Ethernet frame + type + COBS */
#endif

typedef struct {
	uint8_t buf[COBS_RX_CAP]; /* accumulated encoded bytes (no delimiter) */
	size_t  len;
	bool    overflow;         /* a frame exceeded the buffer; drop to next delim */
} cobs_rx_t;

void cobs_rx_init(cobs_rx_t *rx);

/*
 * Feed one received byte. Returns:
 *   0  : byte accumulated, no complete frame yet
 *  >0  : a frame completed; its decoded bytes are in `out` and the return
 *        value is the decoded length (1..out_cap)
 *  -1  : a frame completed but was malformed / overflowed (nothing usable);
 *        the reassembler has reset and is ready for the next frame
 */
int cobs_rx_byte(cobs_rx_t *rx, uint8_t b, uint8_t *out, size_t out_cap);

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_AWDL_COBS_H */
