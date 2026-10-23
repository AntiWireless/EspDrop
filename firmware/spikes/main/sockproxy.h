/*
 * EspDrop - socket proxy between the host and the device's own IP stack (M4).
 *
 * In IP mode the host does not see packets but sockets (docs/M4_ENTWURF.md,
 * sections 3.3 and 4): it opens a UDP port on the AWDL interface, sends
 * datagrams to an address and gets the ones that arrive, with sender and
 * whether they came to a multicast group. That is all mDNS needs (M4.2); TCP
 * follows in M4.3.
 *
 * Host messages are handed in from the bridge RX task, copied and processed on
 * the lwIP thread, which owns all socket state; device messages go out with
 * bridge_send from there.
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

/* A host message of type UDP_OPEN / UDP_SEND / UDP_CLOSE (bridge RX task). */
void sockproxy_handle(uint8_t type, const uint8_t *payload, size_t len);

/* Close every socket (new HELLO, leaving IP mode). Waits for the lwIP thread. */
void sockproxy_reset(void);

/* One-line status for the `ip` command. */
int sockproxy_format_status(char *buf, size_t cap);

#ifdef __cplusplus
}
#endif
