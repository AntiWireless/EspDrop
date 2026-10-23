/*
 * EspDrop - the AWDL link as an lwIP network interface. See awdl_netif.h.
 *
 * Threads: awdl_netif_input runs on the Wi-Fi task and only copies the frame
 * into a pbuf and posts it to lwIP (tcpip_input). Everything that touches lwIP
 * state (bring-up, output, the ping) runs on the tcpip thread. The learned
 * neighbour table is shared between the two and guarded by a spinlock.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_netif.h"

#include <stdio.h>
#include <string.h>

#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "lwip/ethip6.h"
#include "lwip/ip6.h"
#include "lwip/ip6_addr.h"
#include "lwip/mld6.h"
#include "lwip/netif.h"
#include "lwip/pbuf.h"
#include "lwip/prot/icmp6.h"
#include "lwip/raw.h"
#include "lwip/tcpip.h"
#include "lwip/timeouts.h"
#include "netif/ethernet.h"

#include "awdl_app.h"   /* awdl_app_tx_ethernet: the AWDL data TX path */
#include "bridge.h"     /* bridge_log: ping results go to the host log */

#ifndef ETHERTYPE_IPV6
#define ETHERTYPE_IPV6 0x86dd
#endif
#define NB_SLOTS       16       /* learned peers; AirDrop talks to one or two */

static struct netif   s_nif;
static bool           s_created;
static volatile bool  s_up;
static uint8_t        s_mac[6];
static char           s_ll_str[48];   /* our link-local, for `ip` */

/* Counters, read by `ip`. Aligned 32-bit, stats only. */
static volatile uint32_t s_rx_in, s_rx_not_ours, s_rx_nomem, s_rx_full;
static volatile uint32_t s_tx_out, s_tx_fail, s_tx_learned, s_tx_nd, s_tx_mcast;

/* Learned neighbours: source link-local -> source MAC of received frames. */
typedef struct {
	bool     used;
	uint8_t  ip[16];
	uint8_t  mac[6];
	int64_t  seen_us;
} nb_entry_t;
static nb_entry_t   s_nb[NB_SLOTS];
static portMUX_TYPE s_nb_lock = portMUX_INITIALIZER_UNLOCKED;

static void nb_learn(const uint8_t ip[16], const uint8_t mac[6], int64_t now) {
	portENTER_CRITICAL(&s_nb_lock);
	int slot = -1, oldest = 0;
	for (int i = 0; i < NB_SLOTS; i++) {
		if (s_nb[i].used && memcmp(s_nb[i].ip, ip, 16) == 0) { slot = i; break; }
		if (!s_nb[i].used && slot < 0) slot = i;
		if (s_nb[i].used && s_nb[i].seen_us < s_nb[oldest].seen_us) oldest = i;
	}
	if (slot < 0) slot = oldest;   /* full: replace the least recently heard */
	s_nb[slot].used = true;
	memcpy(s_nb[slot].ip, ip, 16);
	memcpy(s_nb[slot].mac, mac, 6);
	s_nb[slot].seen_us = now;
	portEXIT_CRITICAL(&s_nb_lock);
}

static bool nb_lookup(const uint8_t ip[16], uint8_t mac[6]) {
	bool found = false;
	portENTER_CRITICAL(&s_nb_lock);
	for (int i = 0; i < NB_SLOTS && !found; i++)
		if (s_nb[i].used && memcmp(s_nb[i].ip, ip, 16) == 0) {
			memcpy(mac, s_nb[i].mac, 6);
			found = true;
		}
	portEXIT_CRITICAL(&s_nb_lock);
	return found;
}

/* ---- lwIP side (tcpip thread) ------------------------------------------ */

static err_t linkoutput(struct netif *nif, struct pbuf *p) {
	(void)nif;
	static uint8_t frame[14 + AWDL_NETIF_MTU];   /* tcpip thread only */
	if (!s_up || p->tot_len > sizeof(frame)) { s_tx_fail++; return ERR_IF; }
	pbuf_copy_partial(p, frame, p->tot_len, 0);
	if (awdl_app_tx_ethernet(frame, p->tot_len, NULL) != 0) {
		s_tx_fail++;   /* AWDL TX queue full: TCP retransmits, ICMP/UDP is lost */
		return ERR_IF;
	}
	s_tx_out++;
	return ERR_OK;
}

static err_t output_ip6(struct netif *nif, struct pbuf *p, const ip6_addr_t *dst) {
	struct eth_addr to;
	if (ip6_addr_ismulticast(dst)) {
		to.addr[0] = 0x33;   /* RFC 2464: 33:33 + low 32 bits of the group */
		to.addr[1] = 0x33;
		memcpy(&to.addr[2], &dst->addr[3], 4);
		s_tx_mcast++;
	} else if (nb_lookup((const uint8_t *)dst->addr, to.addr)) {
		s_tx_learned++;
	} else {
		s_tx_nd++;   /* never heard from it: lwIP's neighbour discovery */
		return ethip6_output(nif, p, dst);
	}
	return ethernet_output(nif, p, (const struct eth_addr *)nif->hwaddr, &to, ETHTYPE_IPV6);
}

#if LWIP_IPV4
static err_t output_ip4(struct netif *nif, struct pbuf *p, const ip4_addr_t *dst) {
	(void)nif; (void)p; (void)dst;
	return ERR_RTE;   /* AWDL carries IPv6 only */
}
#endif

static err_t netif_init_cb(struct netif *nif) {
	nif->name[0] = 'a';
	nif->name[1] = 'w';
	nif->hwaddr_len = ETH_HWADDR_LEN;
	memcpy(nif->hwaddr, s_mac, 6);
	nif->mtu = AWDL_NETIF_MTU;
#if LWIP_ND6_ALLOW_RA_UPDATES
	nif->mtu6 = AWDL_NETIF_MTU;
#endif
	nif->flags = NETIF_FLAG_BROADCAST | NETIF_FLAG_ETHERNET | NETIF_FLAG_MLD6;
	nif->linkoutput = linkoutput;
	nif->output_ip6 = output_ip6;
#if LWIP_IPV4
	nif->output = output_ip4;
#endif
	return ERR_OK;
}

/* One bring-up/down request at a time (only the bridge task asks), static so a
 * late-running callback after a timeout never writes into a dead stack frame. */
typedef struct {
	bool              up;
	int               err;
	SemaphoreHandle_t done;
} updown_t;
static updown_t s_req;

static void join(struct netif *nif, const ip6_addr_t *group) {
	ip6_addr_t g;
	ip6_addr_copy(g, *group);
	ip6_addr_assign_zone(&g, IP6_MULTICAST, nif);
	mld6_joingroup_netif(nif, &g);
}

static void do_updown(void *arg) {
	updown_t *u = arg;
	u->err = 0;
	if (!s_created) {
		if (!netif_add_noaddr(&s_nif, NULL, netif_init_cb, tcpip_input)) {
			u->err = -1;
			xSemaphoreGive(u->done);
			return;
		}
		/* EUI-64 link-local from the MAC, as Apple's awdl0 and our host bridge
		 * (addr_gen_mode=0) use; valid at once: no DAD on this link. */
		netif_create_ip6_linklocal_address(&s_nif, 1);
		netif_ip6_addr_set_state(&s_nif, 0, IP6_ADDR_PREFERRED);
		ip6addr_ntoa_r(netif_ip6_addr(&s_nif, 0), s_ll_str, sizeof(s_ll_str));
		s_created = true;
	}
	if (u->up && !netif_is_up(&s_nif)) {
		/* Up before netif_set_up: the MLD reports for the joins below must not
		 * meet a linkoutput that still drops everything. */
		s_up = true;
		netif_set_link_up(&s_nif);
		netif_set_up(&s_nif);
#if LWIP_IPV6_SEND_ROUTER_SOLICIT
		s_nif.rs_count = 0;   /* no routers on AWDL; save the airtime */
#endif
		ip6_addr_t g;
		ip6_addr_set_solicitednode(&g, netif_ip6_addr(&s_nif, 0)->addr[3]);
		join(&s_nif, &g);      /* our solicited-node group, for NS from peers */
		IP6_ADDR(&g, PP_HTONL(0xff020000UL), 0, 0, PP_HTONL(0x000000fbUL));
		join(&s_nif, &g);      /* ff02::fb, mDNS */
	} else if (!u->up && netif_is_up(&s_nif)) {
		netif_set_down(&s_nif);
		netif_set_link_down(&s_nif);
	}
	s_up = u->up && netif_is_up(&s_nif);
	xSemaphoreGive(u->done);
}

int awdl_netif_set_up(const uint8_t mac[6], bool up) {
	if (!s_created) memcpy(s_mac, mac, 6);
	if (!s_req.done && !(s_req.done = xSemaphoreCreateBinary())) return -1;
	xSemaphoreTake(s_req.done, 0);   /* drop a give left over from a timed-out call */
	s_req.up = up;
	s_req.err = 0;
	if (tcpip_callback(do_updown, &s_req) != ERR_OK ||
	    xSemaphoreTake(s_req.done, pdMS_TO_TICKS(2000)) != pdTRUE)
		return -1;
	return s_req.err;
}

bool awdl_netif_is_up(void) { return s_up; }

/* ---- receive (Wi-Fi task) ---------------------------------------------- */

void awdl_netif_input(const uint8_t dst[6], const uint8_t src[6], uint16_t ethertype,
                      const uint8_t *payload, size_t plen) {
	if (!s_up || ethertype != ETHERTYPE_IPV6 || plen < 40 || plen > AWDL_NETIF_MTU)
		return;
	/* The radio hears every AWDL data frame; keep only ours and group frames. */
	if (!(dst[0] & 0x01) && memcmp(dst, s_mac, 6) != 0) {
		s_rx_not_ours++;
		return;
	}
	/* Learn the sender: unicast MAC, link-local source (fe80::/10). */
	const uint8_t *sip = payload + 8;
	if (!(src[0] & 0x01) && sip[0] == 0xfe && (sip[1] & 0xc0) == 0x80)
		nb_learn(sip, src, esp_timer_get_time());

	struct pbuf *p = pbuf_alloc(PBUF_RAW, (u16_t)(14 + plen), PBUF_RAM);
	if (!p) { s_rx_nomem++; return; }
	uint8_t *q = p->payload;
	memcpy(q, dst, 6);
	memcpy(q + 6, src, 6);
	q[12] = (uint8_t)(ethertype >> 8);
	q[13] = (uint8_t)(ethertype & 0xff);
	memcpy(q + 14, payload, plen);
	if (s_nif.input(p, &s_nif) != ERR_OK) {   /* tcpip_input: mailbox full */
		pbuf_free(p);
		s_rx_full++;
		return;
	}
	s_rx_in++;
}

/* ---- status --------------------------------------------------------------- */

int awdl_netif_format_status(char *buf, size_t cap) {
	int o = snprintf(buf, cap,
	                 "%s %s rx=%u not_ours=%u nomem=%u full=%u "
	                 "tx=%u fail=%u learned=%u nd=%u mcast=%u peers:",
	                 s_up ? "up" : "down", s_created ? s_ll_str : "-",
	                 (unsigned)s_rx_in, (unsigned)s_rx_not_ours, (unsigned)s_rx_nomem,
	                 (unsigned)s_rx_full, (unsigned)s_tx_out, (unsigned)s_tx_fail,
	                 (unsigned)s_tx_learned, (unsigned)s_tx_nd, (unsigned)s_tx_mcast);
	nb_entry_t nb[NB_SLOTS];
	portENTER_CRITICAL(&s_nb_lock);
	memcpy(nb, s_nb, sizeof(nb));
	portEXIT_CRITICAL(&s_nb_lock);
	int64_t now = esp_timer_get_time();
	int shown = 0;
	for (int i = 0; i < NB_SLOTS && o > 0 && (size_t)o < cap; i++) {
		if (!nb[i].used) continue;
		if (++shown > 4) {
			o += snprintf(buf + o, cap - (size_t)o, " ...");
			break;
		}
		ip6_addr_t a;
		memcpy(a.addr, nb[i].ip, 16);
		char s[48];
		ip6addr_ntoa_r(&a, s, sizeof(s));
		o += snprintf(buf + o, cap - (size_t)o, " %s=%02x:%02x:%02x:%02x:%02x:%02x(%us)", s,
		              nb[i].mac[0], nb[i].mac[1], nb[i].mac[2], nb[i].mac[3],
		              nb[i].mac[4], nb[i].mac[5], (unsigned)((now - nb[i].seen_us) / 1000000));
	}
	if (!shown && o > 0 && (size_t)o < cap) o += snprintf(buf + o, cap - (size_t)o, " none");
	return o;
}

/* ---- ping6 (tcpip thread) ---------------------------------------------- */

#define PING_ID      0xed04
#define PING_DATA    32         /* bytes after the 8-byte echo header */
#define PING_WAIT_MS 2000       /* after the last request, for late replies */

static struct raw_pcb *s_ping_pcb;
static volatile bool   s_ping_busy;
static ip6_addr_t      s_ping_dst;
static int             s_ping_left, s_ping_sent, s_ping_recv;
static uint16_t        s_ping_seq;
static char            s_ping_dst_str[48];

static u8_t ping_recv(void *arg, struct raw_pcb *pcb, struct pbuf *p, const ip_addr_t *addr) {
	(void)arg; (void)pcb; (void)addr;
	uint8_t h[8 + 8];   /* echo header + our send timestamp */
	if (p->tot_len < IP6_HLEN + sizeof(h) ||
	    pbuf_copy_partial(p, h, sizeof(h), IP6_HLEN) != sizeof(h))
		return 0;
	if (h[0] != ICMP6_TYPE_EREP || ((h[4] << 8) | h[5]) != PING_ID) return 0;
	int64_t sent;
	memcpy(&sent, h + 8, sizeof(sent));
	int64_t rtt = esp_timer_get_time() - sent;
	uint8_t hlim = 0;
	pbuf_copy_partial(p, &hlim, 1, 7);
	s_ping_recv++;
	bridge_log("ping6: reply from %s seq %u time %u.%u ms hlim %u", s_ping_dst_str,
	           (unsigned)((h[6] << 8) | h[7]), (unsigned)(rtt / 1000),
	           (unsigned)((rtt % 1000) / 100), hlim);
	pbuf_free(p);
	return 1;   /* ours: eaten */
}

static void ping_done(void *arg) {
	(void)arg;
	bridge_log("ping6: %s %d sent, %d replies, %d%% loss", s_ping_dst_str, s_ping_sent,
	           s_ping_recv, s_ping_sent ? 100 * (s_ping_sent - s_ping_recv) / s_ping_sent : 0);
	s_ping_busy = false;
}

static void ping_send(void *arg) {
	(void)arg;
	if (s_ping_left <= 0) {
		sys_timeout(PING_WAIT_MS, ping_done, NULL);
		return;
	}
	struct pbuf *p = pbuf_alloc(PBUF_IP, 8 + PING_DATA, PBUF_RAM);
	if (p) {
		uint8_t *q = p->payload;
		memset(q, 0, 8 + PING_DATA);
		q[0] = ICMP6_TYPE_EREQ;
		q[4] = PING_ID >> 8;
		q[5] = PING_ID & 0xff;
		q[6] = (uint8_t)(s_ping_seq >> 8);
		q[7] = (uint8_t)s_ping_seq;
		int64_t now = esp_timer_get_time();
		memcpy(q + 8, &now, sizeof(now));
		ip_addr_t to;
		ip_addr_copy_from_ip6(to, s_ping_dst);
		err_t e = raw_sendto(s_ping_pcb, p, &to);   /* lwIP fills the checksum */
		pbuf_free(p);
		if (e != ERR_OK) bridge_log("ping6: send seq %u failed (%d)", s_ping_seq, e);
	}
	s_ping_sent++;
	s_ping_seq++;
	s_ping_left--;
	sys_timeout(1000, ping_send, NULL);
}

static void ping_start(void *arg) {
	(void)arg;
	if (!s_ping_pcb) {
		s_ping_pcb = raw_new_ip_type(IPADDR_TYPE_V6, IP6_NEXTH_ICMP6);
		if (!s_ping_pcb) {
			bridge_log("ping6: no memory for the ICMPv6 socket");
			s_ping_busy = false;
			return;
		}
		s_ping_pcb->chksum_reqd = 1;     /* ICMPv6 checksum covers a pseudo-header */
		s_ping_pcb->chksum_offset = 2;
		raw_recv(s_ping_pcb, ping_recv, NULL);
		raw_bind_netif(s_ping_pcb, &s_nif);
	}
	ping_send(NULL);
}

int awdl_netif_ping6(const char *addr, int count, char *err, size_t errcap) {
	if (!s_up) { snprintf(err, errcap, "interface down (mode l2?)"); return -1; }
	if (s_ping_busy) { snprintf(err, errcap, "a ping is still running"); return -1; }
	char tmp[48];
	snprintf(tmp, sizeof(tmp), "%s", addr);
	char *pct = strchr(tmp, '%');
	if (pct) *pct = '\0';
	ip6_addr_t a;
	if (!ip6addr_aton(tmp, &a)) { snprintf(err, errcap, "not an IPv6 address: %s", tmp); return -1; }
	ip6_addr_assign_zone(&a, IP6_UNICAST, &s_nif);   /* link-local: this interface */
	if (count < 1) count = 1;
	if (count > 100) count = 100;
	s_ping_busy = true;
	ip6_addr_copy(s_ping_dst, a);
	snprintf(s_ping_dst_str, sizeof(s_ping_dst_str), "%s", tmp);
	s_ping_left = count;
	s_ping_sent = s_ping_recv = 0;
	if (tcpip_callback(ping_start, NULL) != ERR_OK) {
		s_ping_busy = false;
		snprintf(err, errcap, "lwIP busy");
		return -1;
	}
	return 0;
}
