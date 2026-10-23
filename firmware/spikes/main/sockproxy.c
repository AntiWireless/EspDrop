/*
 * EspDrop - socket proxy between the host and the device's own IP stack. See
 * sockproxy.h. Wire formats: main/bridge.h, docs/M4_ENTWURF.md section 4.1.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "sockproxy.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "lwip/ip6_addr.h"
#include "lwip/pbuf.h"
#include "lwip/tcpip.h"
#include "lwip/udp.h"

#include "awdl_netif.h"
#include "bridge.h"

#define UDP_SLOTS     2          /* mDNS (5353) and one spare */
#define UDP_HDR_RECV  (2 + 16 + 2 + 1)
#define UDP_HDR_SEND  (2 + 16 + 2)
#define UDP_MAX_DATA  AWDL_NETIF_MTU   /* nothing bigger crosses AWDL anyway */

typedef struct {
	struct udp_pcb *pcb;
	uint16_t        port;
} udp_slot_t;
static udp_slot_t s_udp[UDP_SLOTS];   /* lwIP thread only */

/* Counters for `ip`. Aligned 32-bit, stats only. */
static volatile uint32_t s_udp_rx, s_udp_tx, s_udp_err;

/* A host message, copied for the lwIP thread. */
typedef struct {
	uint8_t type;
	size_t  len;
	uint8_t data[];
} job_t;

static void send_result(uint8_t req, uint16_t id, int8_t err) {
	uint8_t r[4] = { req, (uint8_t)(id & 0xff), (uint8_t)(id >> 8), (uint8_t)err };
	bridge_send(BRIDGE_MSG_RESULT, r, sizeof(r));
}

static udp_slot_t *slot_for(uint16_t port) {
	for (int i = 0; i < UDP_SLOTS; i++)
		if (s_udp[i].pcb && s_udp[i].port == port) return &s_udp[i];
	return NULL;
}

static void close_slot(udp_slot_t *s) {
	if (s->pcb) udp_remove(s->pcb);
	s->pcb = NULL;
	s->port = 0;
}

/* lwIP thread: a datagram for one of our ports -> UDP_RECV to the host. */
static void udp_rx(void *arg, struct udp_pcb *pcb, struct pbuf *p,
                   const ip_addr_t *addr, u16_t port) {
	(void)pcb;
	udp_slot_t *s = arg;
	static uint8_t msg[UDP_HDR_RECV + UDP_MAX_DATA];   /* lwIP thread only */
	if (!IP_IS_V6(addr) || p->tot_len > UDP_MAX_DATA) {
		s_udp_err++;
		pbuf_free(p);
		return;
	}
	msg[0] = (uint8_t)(s->port & 0xff);
	msg[1] = (uint8_t)(s->port >> 8);
	memcpy(msg + 2, ip_2_ip6(addr)->addr, 16);
	msg[18] = (uint8_t)(port & 0xff);
	msg[19] = (uint8_t)(port >> 8);
	msg[20] = ip6_addr_ismulticast(ip_2_ip6(ip_current_dest_addr())) ? 0x01 : 0x00;
	pbuf_copy_partial(p, msg + UDP_HDR_RECV, p->tot_len, 0);
	size_t n = UDP_HDR_RECV + p->tot_len;
	pbuf_free(p);
	if (bridge_send(BRIDGE_MSG_UDP_RECV, msg, n) == 0) s_udp_rx++;
	else s_udp_err++;
}

static void do_open(const uint8_t *d, size_t n) {
	if (n < 3) { send_result(BRIDGE_MSG_UDP_OPEN, 0, SOCKPROXY_EBADREQ); return; }
	uint16_t port = (uint16_t)(d[0] | (d[1] << 8));
	struct netif *nif = awdl_netif_get();
	if (!nif || !awdl_netif_is_up()) { send_result(BRIDGE_MSG_UDP_OPEN, port, SOCKPROXY_EDOWN); return; }
	if (slot_for(port)) { send_result(BRIDGE_MSG_UDP_OPEN, port, SOCKPROXY_OK); return; }
	udp_slot_t *s = NULL;
	for (int i = 0; i < UDP_SLOTS && !s; i++)
		if (!s_udp[i].pcb) s = &s_udp[i];
	if (!s) { send_result(BRIDGE_MSG_UDP_OPEN, port, SOCKPROXY_ENOSLOT); return; }
	struct udp_pcb *pcb = udp_new_ip_type(IPADDR_TYPE_V6);
	if (!pcb) { send_result(BRIDGE_MSG_UDP_OPEN, port, SOCKPROXY_ENOMEM); return; }
	udp_bind_netif(pcb, nif);
	if (udp_bind(pcb, IP6_ADDR_ANY, port) != ERR_OK) {
		udp_remove(pcb);
		send_result(BRIDGE_MSG_UDP_OPEN, port, SOCKPROXY_EFAIL);
		return;
	}
	/* mDNS wants hop limit 255 both ways (RFC 6762 11). The ff02::fb
	 * membership (flags bit 0) is held by the interface itself. */
	pcb->ttl = 255;
	udp_set_multicast_ttl(pcb, 255);
	s->pcb = pcb;
	s->port = port;
	udp_recv(pcb, udp_rx, s);
	send_result(BRIDGE_MSG_UDP_OPEN, port, SOCKPROXY_OK);
}

static void do_send(const uint8_t *d, size_t n) {
	if (n < UDP_HDR_SEND || n - UDP_HDR_SEND > UDP_MAX_DATA) {
		s_udp_err++;
		send_result(BRIDGE_MSG_UDP_SEND, 0, SOCKPROXY_EBADREQ);
		return;
	}
	uint16_t port = (uint16_t)(d[0] | (d[1] << 8));
	udp_slot_t *s = slot_for(port);
	if (!s) {   /* failures only: a RESULT per datagram would double the traffic */
		s_udp_err++;
		send_result(BRIDGE_MSG_UDP_SEND, port, awdl_netif_is_up() ? SOCKPROXY_ENOTOPEN : SOCKPROXY_EDOWN);
		return;
	}
	ip_addr_t to;
	IP_SET_TYPE_VAL(to, IPADDR_TYPE_V6);
	memcpy(ip_2_ip6(&to)->addr, d + 2, 16);
	ip6_addr_assign_zone(ip_2_ip6(&to), IP6_UNKNOWN, awdl_netif_get());   /* link scope: AWDL */
	uint16_t dport = (uint16_t)(d[18] | (d[19] << 8));
	size_t len = n - UDP_HDR_SEND;
	struct pbuf *p = pbuf_alloc(PBUF_TRANSPORT, (u16_t)len, PBUF_RAM);
	if (!p) {
		s_udp_err++;
		send_result(BRIDGE_MSG_UDP_SEND, port, SOCKPROXY_ENOMEM);
		return;
	}
	memcpy(p->payload, d + UDP_HDR_SEND, len);
	err_t e = udp_sendto(s->pcb, p, &to, dport);
	pbuf_free(p);
	if (e == ERR_OK) {
		s_udp_tx++;
	} else {
		s_udp_err++;
		send_result(BRIDGE_MSG_UDP_SEND, port, SOCKPROXY_EFAIL);
	}
}

static void job_run(void *arg) {
	job_t *j = arg;
	switch (j->type) {
	case BRIDGE_MSG_UDP_OPEN:
		do_open(j->data, j->len);
		break;
	case BRIDGE_MSG_UDP_SEND:
		do_send(j->data, j->len);
		break;
	case BRIDGE_MSG_UDP_CLOSE:
		if (j->len >= 2) {
			udp_slot_t *s = slot_for((uint16_t)(j->data[0] | (j->data[1] << 8)));
			if (s) close_slot(s);
		}
		break;
	default:
		break;
	}
	free(j);
}

void sockproxy_handle(uint8_t type, const uint8_t *payload, size_t len) {
	job_t *j = malloc(sizeof(*j) + len);
	if (!j) {
		s_udp_err++;
		send_result(type, 0, SOCKPROXY_ENOMEM);
		return;
	}
	j->type = type;
	j->len = len;
	memcpy(j->data, payload, len);
	if (tcpip_callback(job_run, j) != ERR_OK) {
		free(j);
		s_udp_err++;
		send_result(type, 0, SOCKPROXY_ENOMEM);
	}
}

static SemaphoreHandle_t s_reset_done;

static void do_reset(void *arg) {
	(void)arg;
	for (int i = 0; i < UDP_SLOTS; i++) close_slot(&s_udp[i]);
	xSemaphoreGive(s_reset_done);
}

void sockproxy_reset(void) {
	if (!s_reset_done && !(s_reset_done = xSemaphoreCreateBinary())) return;
	xSemaphoreTake(s_reset_done, 0);
	if (tcpip_callback(do_reset, NULL) == ERR_OK)
		xSemaphoreTake(s_reset_done, pdMS_TO_TICKS(1000));
}

int sockproxy_format_status(char *buf, size_t cap) {
	int o = snprintf(buf, cap, "udp:");
	for (int i = 0; i < UDP_SLOTS && o > 0 && (size_t)o < cap; i++)
		if (s_udp[i].pcb) o += snprintf(buf + o, cap - (size_t)o, " %u", s_udp[i].port);
	if (o > 0 && (size_t)o < cap)
		o += snprintf(buf + o, cap - (size_t)o, " rx=%u tx=%u err=%u",
		              (unsigned)s_udp_rx, (unsigned)s_udp_tx, (unsigned)s_udp_err);
	return o;
}
