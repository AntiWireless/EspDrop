/*
 * EspDrop - USB host-link bridge transport (ESP32).
 *
 * A COBS-framed, type-tagged message stream over the built-in USB-Serial/JTAG,
 * replacing the interactive console when the firmware is built in bridge mode
 * (CONFIG_ESPDROP_BRIDGE_MODE). The platform-independent framing lives in
 * awdl_cobs.*; this module owns the USB driver, the RX task and the dispatch.
 *
 * M2.0 added ECHO (host-side transport test) and LOG. M2.1 added CMD/CMD_REPLY
 * (mac, stats, ping, testframe) and FRAME accounting. M2.2 forwards received
 * AWDL data frames to the host as Ethernet (device->host FRAME); host->device
 * FRAME transmit over the radio and PEER_* come in later M2 stages.
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

#include <stdbool.h>
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
	BRIDGE_MSG_HELLO       = 9, /* host -> device: u8 version, u8 mode (M4) */
	BRIDGE_MSG_HELLO_REPLY = 10,/* device -> host: version, mode, mac, limits, caps */
	/* Socket proxy, IP mode (docs/M4_ENTWURF.md section 4, main/sockproxy.c) */
	BRIDGE_MSG_UDP_OPEN    = 24, /* host -> device: u16 port, u8 flags */
	BRIDGE_MSG_UDP_RECV    = 25, /* device -> host: u16 port, src[16], u16 sport, u8 flags, data */
	BRIDGE_MSG_UDP_SEND    = 26, /* host -> device: u16 port, dst[16], u16 dport, data */
	BRIDGE_MSG_UDP_CLOSE   = 27, /* host -> device: u16 port */
	BRIDGE_MSG_RESULT      = 28, /* device -> host: u8 request type, u16 id, i8 err */
};

/* docs/M4_ENTWURF.md: L2 bridges Ethernet frames to the host's awdl0 (as up to
 * M3); IP terminates IPv6 on the device (awdl_netif). Selected by HELLO or the
 * `mode` command; L2 after boot, so older host tools keep working. */
enum bridge_mode {
	BRIDGE_MODE_L2 = 0,
	BRIDGE_MODE_IP = 1,
};
#define BRIDGE_PROTO_VERSION 1

/* Start the bridge: install the USB driver, silence ESP_LOG (to reduce
 * corruption of the binary stream; the host resynchronises at the next frame
 * delimiter after any stray bytes), and spawn the RX task. Returns 0 on success. */
int bridge_start(void);

/* Send one framed message. Thread-safe. Returns 0 on success. */
int bridge_send(uint8_t type, const void *payload, size_t len);

/*
 * Forward one received AWDL data frame to the host as an Ethernet frame
 * (dst || src || ethertype || payload). Matches radio_data_handler_t so it can
 * be registered directly with radio_set_data_handler(). Called from Wi-Fi task
 * context: it only builds the frame and enqueues it (drop on overflow), so the
 * actual USB write happens on the bridge's own forwarding task.
 */
void bridge_rx_frame(const uint8_t dst[6], const uint8_t src[6], uint16_t ethertype,
                     const uint8_t *payload, size_t plen, void *ctx);

/*
 * Sink for host->device Ethernet frames (BRIDGE_MSG_FRAME): invoked from the
 * bridge RX task with the raw Ethernet frame. Returns 0 on accept. Registered by
 * app_main to the AWDL data-TX path; if none is set the frame is only accounted.
 */
typedef int (*bridge_frame_sink_t)(const uint8_t *eth, size_t len, void *ctx);
void bridge_set_frame_sink(bridge_frame_sink_t fn, void *ctx);

/*
 * Queue a PEER_ADD (added=true) or PEER_REMOVE notification for the host. Never
 * blocks (drop on overflow): safe to call from the AWDL timing task. A dedicated
 * bridge task performs the USB write.
 */
void bridge_notify_peer(bool added, const uint8_t mac[6]);

/* printf-style LOG message to the host. */
void bridge_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_BRIDGE_H */
