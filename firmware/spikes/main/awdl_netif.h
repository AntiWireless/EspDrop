/*
 * EspDrop - the AWDL link as an lwIP network interface (M4, IP mode).
 *
 * In IP mode the ESP terminates IPv6 itself instead of bridging Ethernet frames
 * to the host's awdl0 (docs/M4_ENTWURF.md, section 3.1):
 *  - the interface carries the device's Wi-Fi MAC and the EUI-64 link-local
 *    derived from it, as Apple peers expect, and joins ff02::fb for mDNS;
 *  - received AWDL data frames (radio deliver path) go into lwIP, frames lwIP
 *    sends go to the slot-gated AWDL data TX path (awdl_app_tx_ethernet);
 *  - peers are resolved from a table learned from received traffic (source
 *    link-local -> source MAC), like the host bridge's permanent neighbour
 *    entries; only unknown destinations fall back to lwIP's neighbour discovery.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define AWDL_NETIF_MTU 1460   /* the most one AWDL data frame carries (awdl0 MTU) */

/* Create the interface (once) with this MAC and bring it up or down. While it
 * is down it neither sends nor accepts anything. Returns 0 or an error code. */
int awdl_netif_set_up(const uint8_t mac[6], bool up);

/* True while the interface is up (IP mode). */
bool awdl_netif_is_up(void);

/* The lwIP interface, NULL before the first awdl_netif_set_up. For code on the
 * tcpip thread (sockets bind to it). */
struct netif;
struct netif *awdl_netif_get(void);

/* Feed one received AWDL MSDU (radio data handler signature, Wi-Fi task
 * context; copies). Frames to other stations are dropped, non-IPv6 too. */
void awdl_netif_input(const uint8_t dst[6], const uint8_t src[6], uint16_t ethertype,
                      const uint8_t *payload, size_t plen);

/* One-line status for the `ip` command: address, counters, learned peers. */
int awdl_netif_format_status(char *buf, size_t cap);

/* Start `count` ICMPv6 echo requests to addr (text, e.g. "fe80::1"; a "%zone"
 * suffix is ignored, the zone is always this interface), one per second.
 * Replies and a summary are reported as device log lines. Returns 0, or -1
 * with a reason in err (bad address, interface down, a ping still running). */
int awdl_netif_ping6(const char *addr, int count, char *err, size_t errcap);

#ifdef __cplusplus
}
#endif
