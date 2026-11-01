/*
 * EspDrop - safe byte reading/writing helpers (platform independent).
 *
 * Small helpers for reading and writing the primitive integer types used in
 * AWDL / IEEE 802.11 frames at explicit byte offsets, without relying on struct
 * packing or the host's alignment/endianness. All functions are bounds-checked
 * against an explicit buffer capacity.
 *
 * Inspired by OWL's src/wire.{c,h} (The Open Wireless Link Project / Milan
 * Stute, GPL-3.0), reimplemented here with a simpler offset-based API.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_AWDL_WIRE_H
#define ESPDROP_AWDL_WIRE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ---- writing: append at *off, advance *off, return false on overflow ---- */

static inline bool wire_put_u8(uint8_t *buf, size_t cap, size_t *off, uint8_t v) {
	if (*off + 1 > cap) return false;
	buf[*off] = v;
	*off += 1;
	return true;
}

static inline bool wire_put_le16(uint8_t *buf, size_t cap, size_t *off, uint16_t v) {
	if (*off + 2 > cap) return false;
	buf[*off + 0] = (uint8_t)(v & 0xff);
	buf[*off + 1] = (uint8_t)((v >> 8) & 0xff);
	*off += 2;
	return true;
}

static inline bool wire_put_be16(uint8_t *buf, size_t cap, size_t *off, uint16_t v) {
	if (*off + 2 > cap) return false;
	buf[*off + 0] = (uint8_t)((v >> 8) & 0xff);
	buf[*off + 1] = (uint8_t)(v & 0xff);
	*off += 2;
	return true;
}

static inline bool wire_put_le32(uint8_t *buf, size_t cap, size_t *off, uint32_t v) {
	if (*off + 4 > cap) return false;
	buf[*off + 0] = (uint8_t)(v & 0xff);
	buf[*off + 1] = (uint8_t)((v >> 8) & 0xff);
	buf[*off + 2] = (uint8_t)((v >> 16) & 0xff);
	buf[*off + 3] = (uint8_t)((v >> 24) & 0xff);
	*off += 4;
	return true;
}

static inline bool wire_put_bytes(uint8_t *buf, size_t cap, size_t *off,
                                  const void *src, size_t n) {
	if (*off + n > cap) return false;
	memcpy(buf + *off, src, n);
	*off += n;
	return true;
}

/* ---- reading: read at absolute offset, return false if out of bounds ---- */

static inline bool wire_get_u8(const uint8_t *buf, size_t len, size_t off, uint8_t *out) {
	if (off + 1 > len) return false;
	if (out) *out = buf[off];
	return true;
}

static inline bool wire_get_le16(const uint8_t *buf, size_t len, size_t off, uint16_t *out) {
	if (off + 2 > len) return false;
	if (out) *out = (uint16_t)(buf[off] | ((uint16_t)buf[off + 1] << 8));
	return true;
}

static inline bool wire_get_be16(const uint8_t *buf, size_t len, size_t off, uint16_t *out) {
	if (off + 2 > len) return false;
	if (out) *out = (uint16_t)(((uint16_t)buf[off] << 8) | buf[off + 1]);
	return true;
}

static inline bool wire_get_le32(const uint8_t *buf, size_t len, size_t off, uint32_t *out) {
	if (off + 4 > len) return false;
	if (out)
		*out = (uint32_t)buf[off] | ((uint32_t)buf[off + 1] << 8) |
		       ((uint32_t)buf[off + 2] << 16) | ((uint32_t)buf[off + 3] << 24);
	return true;
}

static inline bool wire_get_bytes(const uint8_t *buf, size_t len, size_t off,
                                  void *dst, size_t n) {
	if (off + n > len) return false;
	if (dst) memcpy(dst, buf + off, n);
	return true;
}

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_AWDL_WIRE_H */
