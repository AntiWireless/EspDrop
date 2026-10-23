/*
 * EspDrop - socket proxy between the host and the device's own IP stack (M4).
 *
 * In IP mode the host does not see packets but sockets (docs/M4_ENTWURF.md,
 * sections 3.3 and 4): it opens a UDP port on the AWDL interface, sends
 * datagrams to an address and gets the ones that arrive, with sender and
 * whether they came to a multicast group (mDNS, M4.2), and it opens TCP
 * connections to a peer and moves their bytes with credit-based flow control
 * (M4.3, section 4.3).
 *
 * Host messages are handed in from the bridge RX task, copied and processed on
 * the lwIP thread, which owns all socket state; messages to the host go out in
 * order through bridge_post.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Error codes in RESULT (i8), shared with the host app. */
enum sockproxy_err {
	SOCKPROXY_OK       = 0,
	SOCKPROXY_EFAIL    = -1,   /* lwIP refused */
	SOCKPROXY_EDOWN    = -2,   /* not in IP mode / interface down */
	SOCKPROXY_ENOSLOT  = -3,   /* no free socket */
	SOCKPROXY_ENOMEM   = -4,
	SOCKPROXY_EBADREQ  = -5,   /* malformed message */
	SOCKPROXY_ENOTOPEN = -6,   /* port not opened with UDP_OPEN */
};

#define SOCKPROXY_TCP_SLOTS 4   /* reported as max_conns in HELLO_REPLY */

/* A host message of type UDP_* or TCP_* (bridge RX task). */
void sockproxy_handle(uint8_t type, const uint8_t *payload, size_t len);

/* Close every socket (new HELLO, leaving IP mode) and set how many bytes each
 * new TCP connection may send to the host before it grants more (HELLO
 * credit). Waits for the lwIP thread. */
void sockproxy_reset(uint32_t host_credit);

/* One-line status for the `ip` command. */
int sockproxy_format_status(char *buf, size_t cap);

#ifdef __cplusplus
}
#endif
