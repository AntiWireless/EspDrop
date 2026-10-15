/*
 * EspDrop - USB host-link bridge transport (ESP32).
 *
 * A COBS-framed, type-tagged message stream over the built-in USB-Serial/JTAG,
 * replacing the interactive console when the firmware is built in bridge mode
 * (CONFIG_ESPDROP_BRIDGE_MODE). The platform-independent framing lives in
 * awdl_cobs.*; this module owns the USB driver, the RX task and the dispatch.
 *
 * M2.0 handles only ECHO (for a host-side transport test) and LOG. The data and
 * control message types are declared now so the host protocol is fixed, and are
 * wired to AWDL in later M2 stages.
 *
 * Wire format per message: COBS(type_byte || payload) followed by a 0x00
 * delimiter. COBS guarantees the body carries no 0x00, so the delimiter is
 * unambiguous and the stream resynchronises after any stray bytes.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#ifndef ESPDROP_BRIDGE_H
#define ESPDROP_BRIDGE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum bridge_msg_type {
	BRIDGE_MSG_ECHO        = 1, /* host -> device: payload echoed back */
	BRIDGE_MSG_ECHO_REPLY  = 2, /* device -> host: the echoed payload */
	BRIDGE_MSG_LOG         = 3, /* device -> host: UTF-8 log text */
	BRIDGE_MSG_FRAME       = 4, /* both: one Ethernet frame for awdl0 (M2.2+) */
	BRIDGE_MSG_PEER_ADD    = 5, /* device -> host: 6-byte peer MAC appeared */
	BRIDGE_MSG_PEER_REMOVE = 6, /* device -> host: 6-byte peer MAC gone */
	BRIDGE_MSG_CMD         = 7, /* host -> device: console-style command text */
	BRIDGE_MSG_CMD_REPLY   = 8, /* device -> host: command output text */
};

/* Start the bridge: install the USB driver, silence stdout logging (so it can
 * not corrupt the binary stream), and spawn the RX task. Returns 0 on success. */
int bridge_start(void);

/* Send one framed message. Thread-safe. Returns 0 on success. */
int bridge_send(uint8_t type, const void *payload, size_t len);

/* printf-style LOG message to the host. */
void bridge_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_BRIDGE_H */
