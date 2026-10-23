/*
 * EspDrop - socket proxy between the host and the device's own IP stack. See
 * sockproxy.h. Wire formats: main/bridge.h, docs/M4_ENTWURF.md section 4.
 *
 * Everything below runs on the lwIP thread except sockproxy_handle and
 * sockproxy_reset, which only hand work to it. Messages to the host go through
 * bridge_post, the ordered queue the forwarding task drains; when it is full,
 * TCP data waits in lwIP (its receive window stays closed) and the queue's
 * space callback kicks us again.
 *
 * Flow control (section 4.3), per connection:
 *  - device -> host: host_credit bytes may be sent; the host grants more with
 *    TCP_CREDIT once its application read them. lwIP's window is only opened
 *    (tcp_recved) for bytes that went to the host, so a slow host or USB
 *    slows the peer down instead of losing frames.
 *  - host -> device: the host may have as many bytes outstanding as
 *    TCP_OPENED granted (the TCP send buffer); every byte the peer ACKs is
 *    granted again with TCP_CREDIT. Bytes lwIP could not take at once wait in
 *    tx_buf, which the credit bounds.
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
#include "lwip/tcp.h"
#include "lwip/tcpip.h"
#include "lwip/udp.h"

#include "awdl_netif.h"
#include "bridge.h"

#define UDP_SLOTS     2          /* mDNS (5353) and one spare */
#define UDP_HDR_RECV  (2 + 16 + 2 + 1)
#define UDP_HDR_SEND  (2 + 16 + 2)
#define UDP_MAX_DATA  AWDL_NETIF_MTU   /* nothing bigger crosses AWDL anyway */
#define TCP_CHUNK     1400       /* TCP_DATA payload to the host (HELLO max_chunk) */

/* ---- state (lwIP thread only) ---------------------------------------------- */

typedef struct {
	struct udp_pcb *pcb;
	uint16_t        port;
} udp_slot_t;
static udp_slot_t s_udp[UDP_SLOTS];

typedef struct {
	bool            used;
	uint16_t        id;
	struct tcp_pcb *pcb;          /* NULL once lwIP freed it or we closed it */
	uint32_t        host_credit;  /* bytes we may still send to the host */
	struct pbuf    *rx;           /* from the peer, not yet sent to the host */
	bool            rx_fin;       /* the peer closed its side (after rx) */
	bool            fin_posted;   /* ... and the host has been told */
	uint8_t        *tx_buf;       /* from the host, lwIP did not take it yet */
	size_t          tx_len, tx_cap;
	bool            host_fin;     /* the host closed its side (after tx_buf) */
	bool            shut_tx;      /* ... and lwIP has been told */
} tcp_conn_t;
static tcp_conn_t s_tcp[SOCKPROXY_TCP_SLOTS];
static uint32_t   s_host_credit = 65536;   /* from HELLO */
static struct tcp_pcb *s_listen;           /* the one listening pcb, if any */
static uint16_t   s_next_accept = 0x8000;  /* device-allocated ids for accepts */
/* Set when tcp_abort ran: a callback that did that must return ERR_ABRT. */
static bool       s_aborted;

/* Counters for `ip`. Aligned 32-bit, stats only. */
static volatile uint32_t s_udp_rx, s_udp_tx, s_udp_err;
static volatile uint32_t s_tcp_opened, s_tcp_to_host, s_tcp_from_host, s_tcp_err;

/* A host message, copied for the lwIP thread. */
typedef struct {
	uint8_t type;
	size_t  len;
	uint8_t data[];
} job_t;

static inline uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }
static inline void wr16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static inline void wr32(uint8_t *p, uint32_t v) {
	p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}

/* Control messages: through the ordered queue if it has room, else directly
 * (blocking on USB is acceptable for these rare, small ones; none of them may
 * overtake a connection's data - see post_close). */
static void post_ctl(uint8_t type, const void *p, size_t n) {
	if (!bridge_post(type, p, n, NULL, 0)) bridge_send(type, p, n);
}

static void send_result(uint8_t req, uint16_t id, int8_t err) {
	uint8_t r[4] = { req, (uint8_t)(id & 0xff), (uint8_t)(id >> 8), (uint8_t)err };
	post_ctl(BRIDGE_MSG_RESULT, r, sizeof(r));
}

static void zone(ip_addr_t *a, const uint8_t addr16[16]) {
	IP_SET_TYPE_VAL(*a, IPADDR_TYPE_V6);
	memcpy(ip_2_ip6(a)->addr, addr16, 16);
	ip6_addr_assign_zone(ip_2_ip6(a), IP6_UNKNOWN, awdl_netif_get());   /* link scope: AWDL */
}

/* ---- UDP --------------------------------------------------------------------- */

static udp_slot_t *udp_slot_for(uint16_t port) {
	for (int i = 0; i < UDP_SLOTS; i++)
		if (s_udp[i].pcb && s_udp[i].port == port) return &s_udp[i];
	return NULL;
}

static void udp_close_slot(udp_slot_t *s) {
	if (s->pcb) udp_remove(s->pcb);
	s->pcb = NULL;
	s->port = 0;
}

static void udp_rx(void *arg, struct udp_pcb *pcb, struct pbuf *p,
                   const ip_addr_t *addr, u16_t port) {
	(void)pcb;
	udp_slot_t *s = arg;
	static uint8_t data[UDP_MAX_DATA];
	if (!IP_IS_V6(addr) || p->tot_len > UDP_MAX_DATA) {
		s_udp_err++;
		pbuf_free(p);
		return;
	}
	uint8_t hdr[UDP_HDR_RECV];
	wr16(hdr, s->port);
	memcpy(hdr + 2, ip_2_ip6(addr)->addr, 16);
	wr16(hdr + 18, port);
	hdr[20] = ip6_addr_ismulticast(ip_2_ip6(ip_current_dest_addr())) ? 0x01 : 0x00;
	u16_t n = pbuf_copy_partial(p, data, p->tot_len, 0);
	pbuf_free(p);
	/* A datagram may be lost: if the queue is full, it is. */
	if (bridge_post(BRIDGE_MSG_UDP_RECV, hdr, sizeof(hdr), data, n)) s_udp_rx++;
	else s_udp_err++;
}

static void udp_open_job(const uint8_t *d, size_t n) {
	if (n < 3) { send_result(BRIDGE_MSG_UDP_OPEN, 0, SOCKPROXY_EBADREQ); return; }
	uint16_t port = rd16(d);
	struct netif *nif = awdl_netif_get();
	if (!nif || !awdl_netif_is_up()) { send_result(BRIDGE_MSG_UDP_OPEN, port, SOCKPROXY_EDOWN); return; }
	if (udp_slot_for(port)) { send_result(BRIDGE_MSG_UDP_OPEN, port, SOCKPROXY_OK); return; }
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

static void udp_send_job(const uint8_t *d, size_t n) {
	if (n < UDP_HDR_SEND || n - UDP_HDR_SEND > UDP_MAX_DATA) {
		s_udp_err++;
		send_result(BRIDGE_MSG_UDP_SEND, 0, SOCKPROXY_EBADREQ);
		return;
	}
	uint16_t port = rd16(d);
	udp_slot_t *s = udp_slot_for(port);
	if (!s) {   /* failures only: a RESULT per datagram would double the traffic */
		s_udp_err++;
		send_result(BRIDGE_MSG_UDP_SEND, port, awdl_netif_is_up() ? SOCKPROXY_ENOTOPEN : SOCKPROXY_EDOWN);
		return;
	}
	ip_addr_t to;
	zone(&to, d + 2);
	size_t len = n - UDP_HDR_SEND;
	struct pbuf *p = pbuf_alloc(PBUF_TRANSPORT, (u16_t)len, PBUF_RAM);
	if (!p) {
		s_udp_err++;
		send_result(BRIDGE_MSG_UDP_SEND, port, SOCKPROXY_ENOMEM);
		return;
	}
	memcpy(p->payload, d + UDP_HDR_SEND, len);
	err_t e = udp_sendto(s->pcb, p, &to, rd16(d + 18));
	pbuf_free(p);
	if (e == ERR_OK) {
		s_udp_tx++;
	} else {
		s_udp_err++;
		send_result(BRIDGE_MSG_UDP_SEND, port, SOCKPROXY_EFAIL);
	}
}

/* ---- TCP --------------------------------------------------------------------- */

static void tcp_setup(tcp_conn_t *c, struct tcp_pcb *pcb);
static void tcp_post_opened(tcp_conn_t *c, struct tcp_pcb *pcb, uint8_t accepted);

static tcp_conn_t *tcp_find(uint16_t id) {
	for (int i = 0; i < SOCKPROXY_TCP_SLOTS; i++)
		if (s_tcp[i].used && s_tcp[i].id == id) return &s_tcp[i];
	return NULL;
}

/* Detach from lwIP (no more callbacks into this slot) and free the slot. */
static void tcp_release(tcp_conn_t *c, bool abort) {
	if (c->pcb) {
		struct tcp_pcb *pcb = c->pcb;
		tcp_arg(pcb, NULL);
		tcp_recv(pcb, NULL);
		tcp_sent(pcb, NULL);
		tcp_err(pcb, NULL);
		tcp_poll(pcb, NULL, 0);
		if (abort || tcp_close(pcb) != ERR_OK) {   /* close can fail on no memory */
			tcp_abort(pcb);
			s_aborted = true;
		}
	}
	if (c->rx) pbuf_free(c->rx);
	free(c->tx_buf);
	memset(c, 0, sizeof(*c));
}

static void post_close(tcp_conn_t *c, uint8_t kind, int8_t err) {
	uint8_t m[4];
	wr16(m, c->id);
	m[2] = kind;
	m[3] = (uint8_t)err;
	if (kind == 0) {
		/* FIN only through the ordered queue: behind the connection's data. */
		if (bridge_post(BRIDGE_MSG_TCP_CLOSE, m, sizeof(m), NULL, 0)) c->fin_posted = true;
	} else {
		post_ctl(BRIDGE_MSG_TCP_CLOSE, m, sizeof(m));
	}
}

/* Both directions closed: let lwIP finish the close on its own. */
static void tcp_maybe_done(tcp_conn_t *c) {
	if (c->fin_posted && c->shut_tx) tcp_release(c, false);
}

/* Peer -> host: as much buffered data as credit and the queue allow. */
static void tcp_flush_rx(tcp_conn_t *c) {
	static uint8_t chunk[TCP_CHUNK];
	while (c->rx && c->host_credit > 0) {
		u16_t n = c->rx->tot_len;
		if (n > TCP_CHUNK) n = TCP_CHUNK;
		if (n > c->host_credit) n = (u16_t)c->host_credit;
		pbuf_copy_partial(c->rx, chunk, n, 0);
		uint8_t hdr[2];
		wr16(hdr, c->id);
		if (!bridge_post(BRIDGE_MSG_TCP_DATA, hdr, sizeof(hdr), chunk, n))
			return;   /* the queue's space callback brings us back */
		c->rx = pbuf_free_header(c->rx, n);
		if (c->pcb) tcp_recved(c->pcb, n);   /* open the window by what left */
		c->host_credit -= n;
		s_tcp_to_host += n;
	}
	if (!c->rx && c->rx_fin && !c->fin_posted) post_close(c, 0, 0);
	tcp_maybe_done(c);
}

/* Host -> peer: hand lwIP what it takes of tx_buf, then a pending FIN. */
static void tcp_flush_tx(tcp_conn_t *c) {
	if (!c->pcb) return;
	size_t off = 0;
	while (off < c->tx_len) {
		size_t n = c->tx_len - off;
		u16_t room = tcp_sndbuf(c->pcb);
		if (n > room) n = room;
		if (n > 0xffff) n = 0xffff;
		if (n == 0 || tcp_write(c->pcb, c->tx_buf + off, (u16_t)n, TCP_WRITE_FLAG_COPY) != ERR_OK)
			break;   /* full: tcp_sent / poll retries */
		off += n;
	}
	if (off) {
		memmove(c->tx_buf, c->tx_buf + off, c->tx_len - off);
		c->tx_len -= off;
		tcp_output(c->pcb);
	}
	if (c->tx_len == 0 && c->host_fin && !c->shut_tx) {
		if (tcp_shutdown(c->pcb, 0, 1) == ERR_OK) c->shut_tx = true;
	}
}

static err_t tcp_on_recv(void *arg, struct tcp_pcb *pcb, struct pbuf *p, err_t err) {
	(void)pcb; (void)err;
	tcp_conn_t *c = arg;
	if (!c) {   /* released already: nobody wants it */
		if (p) pbuf_free(p);
		return ERR_OK;
	}
	if (!p) {
		c->rx_fin = true;
	} else if (c->rx) {
		pbuf_cat(c->rx, p);
	} else {
		c->rx = p;
	}
	s_aborted = false;
	tcp_flush_rx(c);
	return s_aborted ? ERR_ABRT : ERR_OK;
}

static err_t tcp_on_sent(void *arg, struct tcp_pcb *pcb, u16_t len) {
	(void)pcb;
	tcp_conn_t *c = arg;
	if (!c) return ERR_OK;
	uint8_t m[6];
	wr16(m, c->id);
	wr32(m + 2, len);   /* acked: the host may send that much again */
	post_ctl(BRIDGE_MSG_TCP_CREDIT, m, sizeof(m));
	s_aborted = false;
	tcp_flush_tx(c);
	tcp_maybe_done(c);
	return s_aborted ? ERR_ABRT : ERR_OK;
}

static void tcp_on_err(void *arg, err_t err) {
	tcp_conn_t *c = arg;   /* lwIP has freed the pcb already */
	if (!c) return;
	c->pcb = NULL;
	s_tcp_err++;
	post_close(c, 1, (int8_t)err);
	tcp_release(c, false);
}

static err_t tcp_on_poll(void *arg, struct tcp_pcb *pcb) {
	(void)pcb;
	tcp_conn_t *c = arg;
	s_aborted = false;
	if (c) {
		tcp_flush_tx(c);
		if (c->used) tcp_flush_rx(c);
	}
	return s_aborted ? ERR_ABRT : ERR_OK;
}

static err_t tcp_on_connected(void *arg, struct tcp_pcb *pcb, err_t err) {
	tcp_conn_t *c = arg;
	if (!c) return ERR_OK;
	if (err != ERR_OK) {   /* lwIP reports a failed connect through tcp_err */
		return err;
	}
	tcp_post_opened(c, pcb, 0);   /* we connected, not accepted */
	return ERR_OK;
}

static err_t tcp_on_accept(void *arg, struct tcp_pcb *pcb, err_t err) {
	(void)arg;
	if (err != ERR_OK || !pcb) return ERR_VAL;
	tcp_conn_t *c = NULL;
	for (int i = 0; i < SOCKPROXY_TCP_SLOTS && !c; i++)
		if (!s_tcp[i].used) c = &s_tcp[i];
	if (!c) { tcp_abort(pcb); return ERR_ABRT; }   /* no slot: refuse */
	memset(c, 0, sizeof(*c));
	c->used = true;
	c->id = s_next_accept;
	s_next_accept = s_next_accept < 0xffff ? s_next_accept + 1 : 0x8000;
	c->host_credit = s_host_credit;
	tcp_setup(c, pcb);
	tcp_post_opened(c, pcb, 1);
	return ERR_OK;
}

static void tcp_post_opened(tcp_conn_t *c, struct tcp_pcb *pcb, uint8_t accepted) {
	uint8_t m[2 + 16 + 2 + 2 + 1 + 4];
	wr16(m, c->id);
	memcpy(m + 2, ip_2_ip6(&pcb->remote_ip)->addr, 16);
	wr16(m + 18, pcb->remote_port);
	wr16(m + 20, pcb->local_port);
	m[22] = accepted;
	wr32(m + 23, tcp_sndbuf(pcb));     /* the host may send this much */
	post_ctl(BRIDGE_MSG_TCP_OPENED, m, sizeof(m));
	s_tcp_opened++;
}

static void tcp_setup(tcp_conn_t *c, struct tcp_pcb *pcb) {
	c->pcb = pcb;
	tcp_arg(pcb, c);
	tcp_recv(pcb, tcp_on_recv);
	tcp_sent(pcb, tcp_on_sent);
	tcp_err(pcb, tcp_on_err);
	tcp_poll(pcb, tcp_on_poll, 2);   /* every second: retries */
	tcp_nagle_disable(pcb);          /* HTTP requests are small and waited for */
}

static void tcp_fail(uint16_t id, int8_t err) {
	uint8_t m[4];
	wr16(m, id);
	m[2] = 1;
	m[3] = (uint8_t)err;
	post_ctl(BRIDGE_MSG_TCP_CLOSE, m, sizeof(m));
	s_tcp_err++;
}

static void tcp_connect_job(const uint8_t *d, size_t n) {
	if (n < 20) { tcp_fail(n >= 2 ? rd16(d) : 0, SOCKPROXY_EBADREQ); return; }
	uint16_t id = rd16(d);
	struct netif *nif = awdl_netif_get();
	if (!nif || !awdl_netif_is_up()) { tcp_fail(id, SOCKPROXY_EDOWN); return; }
	if (tcp_find(id)) { tcp_fail(id, SOCKPROXY_EBADREQ); return; }
	tcp_conn_t *c = NULL;
	for (int i = 0; i < SOCKPROXY_TCP_SLOTS && !c; i++)
		if (!s_tcp[i].used) c = &s_tcp[i];
	if (!c) { tcp_fail(id, SOCKPROXY_ENOSLOT); return; }
	struct tcp_pcb *pcb = tcp_new_ip_type(IPADDR_TYPE_V6);
	if (!pcb) { tcp_fail(id, SOCKPROXY_ENOMEM); return; }
	memset(c, 0, sizeof(*c));
	c->used = true;
	c->id = id;
	c->host_credit = s_host_credit;
	tcp_bind_netif(pcb, nif);
	tcp_setup(c, pcb);
	ip_addr_t to;
	zone(&to, d + 2);
	err_t e = tcp_connect(pcb, &to, rd16(d + 18), tcp_on_connected);
	if (e != ERR_OK) {
		tcp_release(c, true);
		tcp_fail(id, SOCKPROXY_EFAIL);
	}
}

static void tcp_listen_job(const uint8_t *d, size_t n) {
	if (n < 2) { send_result(BRIDGE_MSG_TCP_LISTEN, 0, SOCKPROXY_EBADREQ); return; }
	uint16_t port = rd16(d);
	struct netif *nif = awdl_netif_get();
	if (!nif || !awdl_netif_is_up()) { send_result(BRIDGE_MSG_TCP_LISTEN, port, SOCKPROXY_EDOWN); return; }
	if (s_listen) { send_result(BRIDGE_MSG_TCP_LISTEN, port, SOCKPROXY_OK); return; }
	struct tcp_pcb *pcb = tcp_new_ip_type(IPADDR_TYPE_V6);
	if (!pcb) { send_result(BRIDGE_MSG_TCP_LISTEN, port, SOCKPROXY_ENOMEM); return; }
	tcp_bind_netif(pcb, nif);
	if (tcp_bind(pcb, IP6_ADDR_ANY, port) != ERR_OK) {
		tcp_close(pcb);
		send_result(BRIDGE_MSG_TCP_LISTEN, port, SOCKPROXY_EFAIL);
		return;
	}
	err_t e = ERR_OK;
	struct tcp_pcb *lpcb = tcp_listen_with_backlog_and_err(pcb, SOCKPROXY_TCP_SLOTS, &e);
	if (!lpcb) {
		tcp_close(pcb);
		send_result(BRIDGE_MSG_TCP_LISTEN, port, SOCKPROXY_EFAIL);
		return;
	}
	s_listen = lpcb;
	tcp_accept(lpcb, tcp_on_accept);
	send_result(BRIDGE_MSG_TCP_LISTEN, port, SOCKPROXY_OK);
}

static void tcp_data_job(const uint8_t *d, size_t n) {
	if (n < 2) return;
	uint16_t id = rd16(d);
	tcp_conn_t *c = tcp_find(id);
	if (!c || !c->pcb || c->host_fin) {   /* gone, or data after the host's FIN */
		if (!c) tcp_fail(id, SOCKPROXY_ENOTOPEN);
		return;
	}
	size_t len = n - 2;
	s_tcp_from_host += len;
	if (c->tx_len + len > c->tx_cap) {   /* the credit bounds this to the send buffer */
		size_t cap = c->tx_len + len;
		uint8_t *b = realloc(c->tx_buf, cap);
		if (!b) {
			post_close(c, 1, SOCKPROXY_ENOMEM);
			tcp_release(c, true);
			return;
		}
		c->tx_buf = b;
		c->tx_cap = cap;
	}
	memcpy(c->tx_buf + c->tx_len, d + 2, len);
	c->tx_len += len;
	tcp_flush_tx(c);
}

static void tcp_credit_job(const uint8_t *d, size_t n) {
	if (n < 6) return;
	tcp_conn_t *c = tcp_find(rd16(d));
	if (!c) return;
	c->host_credit += (uint32_t)(d[2] | (d[3] << 8) | (d[4] << 16) | ((uint32_t)d[5] << 24));
	tcp_flush_rx(c);
}

static void tcp_close_job(const uint8_t *d, size_t n) {
	if (n < 3) return;
	tcp_conn_t *c = tcp_find(rd16(d));
	if (!c) return;
	if (d[2] != 0 || !c->pcb) {   /* RST from the host: drop everything at once */
		tcp_release(c, true);
		return;
	}
	c->host_fin = true;            /* FIN: after whatever is still in tx_buf */
	tcp_flush_tx(c);
	tcp_maybe_done(c);
}

/* The host queue has room again: retry what waited for it. */
static void kick(void *arg) {
	(void)arg;
	for (int i = 0; i < SOCKPROXY_TCP_SLOTS; i++)
		if (s_tcp[i].used) tcp_flush_rx(&s_tcp[i]);
}

static void on_queue_space(void) {   /* forwarding task */
	tcpip_try_callback(kick, NULL);   /* if lwIP's mailbox is full, poll retries */
}

/* ---- dispatch ------------------------------------------------------------------ */

static void job_run(void *arg) {
	job_t *j = arg;
	switch (j->type) {
	case BRIDGE_MSG_UDP_OPEN:    udp_open_job(j->data, j->len); break;
	case BRIDGE_MSG_UDP_SEND:    udp_send_job(j->data, j->len); break;
	case BRIDGE_MSG_UDP_CLOSE:
		if (j->len >= 2) {
			udp_slot_t *s = udp_slot_for(rd16(j->data));
			if (s) udp_close_slot(s);
		}
		break;
	case BRIDGE_MSG_TCP_LISTEN:  tcp_listen_job(j->data, j->len); break;
	case BRIDGE_MSG_TCP_CONNECT: tcp_connect_job(j->data, j->len); break;
	case BRIDGE_MSG_TCP_DATA:    tcp_data_job(j->data, j->len); break;
	case BRIDGE_MSG_TCP_CREDIT:  tcp_credit_job(j->data, j->len); break;
	case BRIDGE_MSG_TCP_CLOSE:   tcp_close_job(j->data, j->len); break;
	default: break;
	}
	free(j);
}

void sockproxy_handle(uint8_t type, const uint8_t *payload, size_t len) {
	job_t *j = malloc(sizeof(*j) + len);
	if (!j) {
		s_tcp_err++;
		send_result(type, 0, SOCKPROXY_ENOMEM);
		return;
	}
	j->type = type;
	j->len = len;
	memcpy(j->data, payload, len);
	/* Blocking post: the host's data must not be dropped here (the credit
	 * already bounds how much can be in flight). */
	if (tcpip_callback(job_run, j) != ERR_OK) {
		free(j);
		s_tcp_err++;
		send_result(type, 0, SOCKPROXY_ENOMEM);
	}
}

static SemaphoreHandle_t s_reset_done;
static uint32_t          s_reset_credit;

static void reset_job(void *arg) {
	(void)arg;
	for (int i = 0; i < UDP_SLOTS; i++) udp_close_slot(&s_udp[i]);
	if (s_listen) { tcp_close(s_listen); s_listen = NULL; }
	for (int i = 0; i < SOCKPROXY_TCP_SLOTS; i++)
		if (s_tcp[i].used) tcp_release(&s_tcp[i], true);
	s_host_credit = s_reset_credit ? s_reset_credit : 65536;
	bridge_set_post_space_cb(on_queue_space);
	xSemaphoreGive(s_reset_done);
}

void sockproxy_reset(uint32_t host_credit) {
	if (!s_reset_done && !(s_reset_done = xSemaphoreCreateBinary())) return;
	xSemaphoreTake(s_reset_done, 0);
	s_reset_credit = host_credit;
	if (tcpip_callback(reset_job, NULL) == ERR_OK)
		xSemaphoreTake(s_reset_done, pdMS_TO_TICKS(1000));
}

int sockproxy_format_status(char *buf, size_t cap) {
	int o = snprintf(buf, cap, "udp:");
	for (int i = 0; i < UDP_SLOTS && o > 0 && (size_t)o < cap; i++)
		if (s_udp[i].pcb) o += snprintf(buf + o, cap - (size_t)o, " %u", s_udp[i].port);
	if (o > 0 && (size_t)o < cap)
		o += snprintf(buf + o, cap - (size_t)o, " rx=%u tx=%u err=%u tcp: open",
		              (unsigned)s_udp_rx, (unsigned)s_udp_tx, (unsigned)s_udp_err);
	int open = 0;
	for (int i = 0; i < SOCKPROXY_TCP_SLOTS; i++)
		if (s_tcp[i].used) open++;
	if (o > 0 && (size_t)o < cap)
		o += snprintf(buf + o, cap - (size_t)o, "=%d opened=%u to_host=%u from_host=%u err=%u",
		              open, (unsigned)s_tcp_opened, (unsigned)s_tcp_to_host,
		              (unsigned)s_tcp_from_host, (unsigned)s_tcp_err);
	return o;
}
