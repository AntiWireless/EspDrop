/*
 * EspDrop - native host unit test for the platform-independent AWDL frame code.
 *
 * Builds PSF / MIF / data frames and parses them back, checking that every
 * field round-trips and that the TLV length fields are internally consistent.
 * Also dumps a MIF as hex so it can be pasted into a Wireshark "import hex dump"
 * and checked against a real capture.
 *
 * Build & run:  make -C firmware/spikes/components/awdl/test/host
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_frame.h"
#include "awdl_sync.h"
#include "awdl_peers.h"
#include "awdl_node.h"
#include "awdl_dedup.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_fail;

#define CHECK(cond, ...) do { \
	if (!(cond)) { printf("FAIL %s:%d: ", __func__, __LINE__); printf(__VA_ARGS__); printf("\n"); g_fail++; } \
} while (0)

/* Walk the TLVs of an action frame and confirm they tile the buffer exactly. */
static void check_tlv_consistency(const uint8_t *buf, int len) {
	int off = IEEE80211_HDR_LEN + 16; /* mac + action header */
	int ntlv = 0;
	while (off + 3 <= len) {
		uint16_t l = (uint16_t)(buf[off + 1] | ((uint16_t)buf[off + 2] << 8));
		off += 3 + l;
		ntlv++;
		CHECK(off <= len, "TLV %d overruns buffer (off=%d len=%d)", ntlv, off, len);
	}
	CHECK(off == len, "TLVs do not tile buffer exactly: off=%d len=%d", off, len);
	CHECK(ntlv >= 6, "expected >=6 TLVs, got %d", ntlv);
}

static void hexdump(const char *label, const uint8_t *buf, int len) {
	printf("%s (%d bytes):\n", label, len);
	for (int i = 0; i < len; i++) {
		printf("%02x", buf[i]);
		if ((i & 15) == 15) printf("\n");
		else printf(" ");
	}
	if (len & 15) printf("\n");
}

static void test_mif_roundtrip(void) {
	uint8_t self_mac[6] = {0x02, 0xaa, 0xbb, 0xcc, 0xdd, 0xee};
	awdl_self_t self;
	awdl_self_init(&self, self_mac, "espdrop-c3", 6);

	uint8_t buf[1500];
	int n = awdl_build_action(buf, sizeof(buf), &self, AWDL_SUBTYPE_MIF, 123456789ull, 64, 0);
	CHECK(n > 0, "build MIF failed: %d", n);

	/* 802.11 action frame control */
	CHECK(buf[0] == 0xd0 && buf[1] == 0x00, "frame control not action: %02x%02x", buf[1], buf[0]);
	/* addr3 == AWDL BSSID */
	CHECK(memcmp(buf + 16, AWDL_BSSID, 6) == 0, "addr3 not AWDL BSSID");
	/* addr1 == broadcast */
	CHECK(memcmp(buf + 4, AWDL_BROADCAST, 6) == 0, "addr1 not broadcast");

	check_tlv_consistency(buf, n);

	awdl_peer_view_t pv;
	int r = awdl_parse_action(buf, (size_t)n, &pv);
	CHECK(r == AWDL_OK, "parse MIF returned %d", r);
	CHECK(memcmp(pv.src_addr, self_mac, 6) == 0, "src addr mismatch");
	CHECK(pv.subtype == AWDL_SUBTYPE_MIF, "subtype %u", pv.subtype);
	CHECK(pv.has_name && strcmp(pv.name, "espdrop-c3") == 0, "name='%s' has=%d", pv.name, pv.has_name);
	CHECK(pv.has_version && pv.version == AWDL_VERSION_OWL, "version %02x has=%d", pv.version, pv.has_version);
	CHECK(pv.has_chanseq, "no chanseq");
	for (int i = 0; i < 16 && pv.has_chanseq; i++)
		CHECK(pv.chanseq[i] == 6, "chanseq[%d]=%u != 6", i, pv.chanseq[i]);
	CHECK(pv.has_election && memcmp(pv.master_addr, self_mac, 6) == 0, "election master mismatch");

	hexdump("MIF", buf, n);
}

static void test_psf_roundtrip(void) {
	uint8_t self_mac[6] = {0x02, 0x11, 0x22, 0x33, 0x44, 0x55};
	awdl_self_t self;
	awdl_self_init(&self, self_mac, "x", 149);

	uint8_t buf[1500];
	int n = awdl_build_action(buf, sizeof(buf), &self, AWDL_SUBTYPE_PSF, 0, 64, 0);
	CHECK(n > 0, "build PSF failed: %d", n);
	check_tlv_consistency(buf, n);

	awdl_peer_view_t pv;
	CHECK(awdl_parse_action(buf, (size_t)n, &pv) == AWDL_OK, "parse PSF");
	CHECK(pv.subtype == AWDL_SUBTYPE_PSF, "subtype %u", pv.subtype);
	/* PSF carries no ARPA/HT per OWL; name should be absent */
	CHECK(!pv.has_name, "PSF unexpectedly has name");
	CHECK(pv.has_chanseq && pv.chanseq[0] == 149, "chanseq[0]=%u", pv.chanseq[0]);
}

static void test_data_roundtrip(void) {
	uint8_t self_mac[6] = {0x02, 0, 0, 0, 0, 0x01};
	uint8_t dst_mac[6]  = {0x02, 0, 0, 0, 0, 0x02};
	awdl_self_t self;
	awdl_self_init(&self, self_mac, "x", 6);

	const uint8_t payload[] = {0x60, 0, 0, 0, 0, 8, 58, 255, /* truncated IPv6-ish */ 1, 2, 3, 4};
	uint8_t buf[1500];
	int n = awdl_build_data(buf, sizeof(buf), &self, dst_mac, payload, sizeof(payload));
	CHECK(n > 0, "build data failed: %d", n);
	CHECK(buf[0] == 0x08 && buf[1] == 0x00, "data frame control %02x%02x", buf[1], buf[0]);
	CHECK(memcmp(buf + 4, dst_mac, 6) == 0, "data addr1 != dst");

	size_t poff; uint16_t et;
	CHECK(awdl_parse_data(buf, (size_t)n, &poff, &et), "parse_data failed");
	CHECK(et == ETHERTYPE_IPV6, "ethertype %04x", et);
	CHECK((int)(poff + sizeof(payload)) == n, "payload length mismatch poff=%zu n=%d", poff, n);
	CHECK(memcmp(buf + poff, payload, sizeof(payload)) == 0, "payload mismatch");
}

/* M2.2: the RX parser must accept real Apple data frames, which may be QoS data
 * (subtype 8, +2-byte QoS control) and may carry 802.11 flag bits. */
static void test_data_qos_and_flags(void) {
	uint8_t self_mac[6] = {0x02, 0, 0, 0, 0, 0x01};
	uint8_t dst_mac[6]  = {0x02, 0, 0, 0, 0, 0x02};
	awdl_self_t self;
	awdl_self_init(&self, self_mac, "x", 6);
	const uint8_t payload[] = {0x60, 0, 0, 0, 0, 8, 58, 255, 1, 2, 3, 4};
	uint8_t buf[1500];
	int n = awdl_build_data(buf, sizeof(buf), &self, dst_mac, payload, sizeof(payload));
	CHECK(n > 0, "build data failed: %d", n);

	size_t poff0; uint16_t et;
	CHECK(awdl_parse_data(buf, (size_t)n, &poff0, &et), "baseline parse failed");

	/* Flag bits set (retry + power-mgmt) must not cause rejection. */
	uint8_t f[1500];
	memcpy(f, buf, (size_t)n);
	f[1] = 0x18;
	size_t poff;
	CHECK(awdl_parse_data(f, (size_t)n, &poff, &et) && poff == poff0 && et == ETHERTYPE_IPV6,
	      "flagged frame rejected");

	/* QoS data: subtype 8, 2-byte QoS control inserted after the 24-byte header.
	 * Payload offset must shift by exactly 2, everything else unchanged. */
	uint8_t q[1500];
	memcpy(q, buf, IEEE80211_HDR_LEN);
	q[0] = 0x88;                               /* data, QoS-data subtype (8) */
	q[IEEE80211_HDR_LEN] = 0x00;               /* QoS control */
	q[IEEE80211_HDR_LEN + 1] = 0x00;
	memcpy(q + IEEE80211_HDR_LEN + 2, buf + IEEE80211_HDR_LEN,
	       (size_t)n - IEEE80211_HDR_LEN);
	int qn = n + 2;
	CHECK(awdl_parse_data(q, (size_t)qn, &poff, &et), "QoS parse failed");
	CHECK(poff == poff0 + 2, "QoS poff %zu != %zu+2", poff, poff0);
	CHECK(et == ETHERTYPE_IPV6, "QoS ethertype %04x", et);
	CHECK(memcmp(q + poff, payload, sizeof(payload)) == 0, "QoS payload mismatch");

	/* A non-data subtype (4 = Null/other) must be rejected. */
	uint8_t bad[1500];
	memcpy(bad, buf, (size_t)n);
	bad[0] = 0x48;
	CHECK(!awdl_parse_data(bad, (size_t)n, &poff, &et), "subtype 4 not rejected");
}

/* M2.3b: 802.11 retransmissions captured in promiscuous mode must be dropped
 * (they showed up as "+N duplicates" in ping), but genuinely new frames not. */
static void test_dedup(void) {
	uint8_t a_mac[6] = {0x02, 0, 0, 0, 0, 0x0a};
	uint8_t b_mac[6] = {0x02, 0, 0, 0, 0, 0x0b};
	uint8_t dst[6]   = {0x02, 0, 0, 0, 0, 0x02};
	const uint8_t payload[] = {0x60, 0, 0, 0, 0, 8, 58, 255, 1, 2, 3, 4};
	awdl_self_t a, b;
	awdl_self_init(&a, a_mac, "a", 6);
	awdl_self_init(&b, b_mac, "b", 6);

	uint8_t f1[200], f2[200], fb[200];
	int n1 = awdl_build_data(f1, sizeof(f1), &a, dst, payload, sizeof(payload));
	int n2 = awdl_build_data(f2, sizeof(f2), &a, dst, payload, sizeof(payload)); /* next seq */
	int nb = awdl_build_data(fb, sizeof(fb), &b, dst, payload, sizeof(payload));
	CHECK(n1 > 0 && n2 > 0 && nb > 0, "build failed");
	/* b's first frame has the same sequence number as a's first frame */
	CHECK(fb[22] == f1[22] && fb[23] == f1[23], "test setup: seq mismatch");

	awdl_dedup_t d;
	awdl_dedup_init(&d);
	CHECK(!awdl_dedup_is_dup(&d, f1, (size_t)n1), "first frame flagged dup");

	uint8_t r1[200];
	memcpy(r1, f1, (size_t)n1);
	r1[1] |= 0x08; /* Retry bit */
	CHECK(awdl_dedup_is_dup(&d, r1, (size_t)n1), "retry of same seq not dropped");
	CHECK(awdl_dedup_is_dup(&d, r1, (size_t)n1), "second retry not dropped");

	/* Same seq without Retry is a new frame per 802.11 (e.g. after seq wrap). */
	CHECK(!awdl_dedup_is_dup(&d, f1, (size_t)n1), "non-retry same seq dropped");

	/* Next sequence number from a, even with Retry set, is new. */
	uint8_t r2[200];
	memcpy(r2, f2, (size_t)n2);
	r2[1] |= 0x08;
	CHECK(!awdl_dedup_is_dup(&d, r2, (size_t)n2), "new seq with retry dropped");

	/* Another sender reusing the same seq is independent. */
	uint8_t rb[200];
	memcpy(rb, fb, (size_t)nb);
	rb[1] |= 0x08;
	CHECK(!awdl_dedup_is_dup(&d, rb, (size_t)nb), "other sender's frame dropped");

	/* QoS: the same seq on a different TID is a different sequence space. */
	uint8_t q0[200], q5[200];
	memcpy(q0, f1, IEEE80211_HDR_LEN);
	q0[0] = 0x88; q0[IEEE80211_HDR_LEN] = 0x00; q0[IEEE80211_HDR_LEN + 1] = 0x00;
	memcpy(q0 + IEEE80211_HDR_LEN + 2, f1 + IEEE80211_HDR_LEN, (size_t)n1 - IEEE80211_HDR_LEN);
	memcpy(q5, q0, (size_t)n1 + 2);
	q5[IEEE80211_HDR_LEN] = 0x05;
	awdl_dedup_init(&d);
	CHECK(!awdl_dedup_is_dup(&d, q0, (size_t)n1 + 2), "QoS tid0 first flagged");
	q5[1] |= 0x08;
	CHECK(!awdl_dedup_is_dup(&d, q5, (size_t)n1 + 2), "QoS tid5 retry wrongly dropped");
	q0[1] |= 0x08;
	CHECK(awdl_dedup_is_dup(&d, q0, (size_t)n1 + 2), "QoS tid0 retry not dropped");

	/* Too short to carry sequence control: never a duplicate, no crash. */
	CHECK(!awdl_dedup_is_dup(&d, f1, 10), "short frame flagged dup");
	/* QoS frame truncated before the QoS control field. */
	CHECK(!awdl_dedup_is_dup(&d, q0, 24), "truncated QoS flagged dup");
}

/* Collects MSDUs delivered by awdl_parse_data_msdus. */
typedef struct {
	int     n;
	uint8_t dst[4][6], src[4][6];
	uint16_t et[4];
	uint8_t pl[4][64];
	size_t  plen[4];
} msdu_sink_t;

static void msdu_collect(const uint8_t dst[6], const uint8_t src[6], uint16_t et,
                         const uint8_t *payload, size_t plen, void *ctx) {
	msdu_sink_t *k = ctx;
	if (k->n >= 4 || plen > sizeof(k->pl[0])) { k->n = 99; return; }
	memcpy(k->dst[k->n], dst, 6);
	memcpy(k->src[k->n], src, 6);
	k->et[k->n] = et;
	memcpy(k->pl[k->n], payload, plen);
	k->plen[k->n] = plen;
	k->n++;
}

/* M2.4: plain and A-MSDU data frames through awdl_parse_data_msdus. An A-MSDU
 * body is subframes DA(6) SA(6) len(2, BE) + MSDU, padded to 4 bytes except
 * after the last (as in OWL's awdl_rx_data_amsdu). */
static void test_data_amsdu(void) {
	uint8_t a_mac[6] = {0x02, 0, 0, 0, 0, 0x0a};
	uint8_t dst[6]   = {0x02, 0, 0, 0, 0, 0x02};
	uint8_t mc[6]    = {0x33, 0x33, 0, 0, 0, 0xfb};
	awdl_self_t a;
	awdl_self_init(&a, a_mac, "a", 6);
	const uint8_t p1[] = {0x60, 1, 2, 3, 4};            /* 5 B: forces 1 B padding */
	const uint8_t p2[] = {0x60, 9, 8, 7, 6, 5, 4, 3};   /* 8 B */

	/* Plain frame: one MSDU, dst/src from addr1/addr2. */
	uint8_t f[300];
	int n = awdl_build_data(f, sizeof(f), &a, dst, p1, sizeof(p1));
	CHECK(n > 0, "build plain");
	msdu_sink_t k;
	memset(&k, 0, sizeof(k));
	CHECK(awdl_parse_data_msdus(f, (size_t)n, msdu_collect, &k) == 1 && k.n == 1,
	      "plain: msdus=%d", k.n);
	CHECK(memcmp(k.dst[0], dst, 6) == 0 && memcmp(k.src[0], a_mac, 6) == 0, "plain addrs");
	CHECK(k.et[0] == ETHERTYPE_IPV6 && k.plen[0] == sizeof(p1) &&
	      memcmp(k.pl[0], p1, sizeof(p1)) == 0, "plain payload");

	/* MSDU bodies (LLC/SNAP + AWDL data header + payload) taken from built
	 * frames: everything after the 24-byte non-QoS header. */
	uint8_t b1[300], b2[300];
	int n1 = awdl_build_data(b1, sizeof(b1), &a, dst, p1, sizeof(p1));
	int n2 = awdl_build_data(b2, sizeof(b2), &a, mc, p2, sizeof(p2));
	size_t m1 = (size_t)n1 - IEEE80211_HDR_LEN, m2 = (size_t)n2 - IEEE80211_HDR_LEN;

	uint8_t q[600];
	size_t off = 0;
	memcpy(q, b1, IEEE80211_HDR_LEN);                     /* addr1/2/3, seq */
	q[0] = 0x88;                                          /* QoS data */
	off = IEEE80211_HDR_LEN;
	q[off++] = 0x80; q[off++] = 0x00;                     /* QoS ctl: A-MSDU present */
	/* subframe 1 */
	memcpy(q + off, dst, 6); memcpy(q + off + 6, a_mac, 6);
	q[off + 12] = (uint8_t)(m1 >> 8); q[off + 13] = (uint8_t)m1;
	memcpy(q + off + 14, b1 + IEEE80211_HDR_LEN, m1);
	off += 14 + m1;
	/* pad so that (14 + m1) rounds up to a multiple of 4 */
	size_t pad = (4 - ((14 + m1) % 4)) % 4;
	off = IEEE80211_HDR_LEN + 2 + 14 + m1 + pad;
	memset(q + IEEE80211_HDR_LEN + 2 + 14 + m1, 0, pad);
	/* subframe 2 (multicast DA), last: no padding */
	memcpy(q + off, mc, 6); memcpy(q + off + 6, a_mac, 6);
	q[off + 12] = (uint8_t)(m2 >> 8); q[off + 13] = (uint8_t)m2;
	memcpy(q + off + 14, b2 + IEEE80211_HDR_LEN, m2);
	off += 14 + m2;

	bool amsdu = false;
	CHECK(awdl_data_hdr_len(q, off, &amsdu) == IEEE80211_HDR_LEN + 2 && amsdu,
	      "A-MSDU not detected");
	CHECK(!awdl_parse_data(q, off, NULL, NULL), "legacy parser must not misread A-MSDU");
	memset(&k, 0, sizeof(k));
	int r = awdl_parse_data_msdus(q, off, msdu_collect, &k);
	CHECK(r == 2 && k.n == 2, "A-MSDU: r=%d msdus=%d", r, k.n);
	CHECK(memcmp(k.dst[0], dst, 6) == 0 && memcmp(k.dst[1], mc, 6) == 0, "A-MSDU dsts");
	CHECK(memcmp(k.src[1], a_mac, 6) == 0, "A-MSDU src");
	CHECK(k.plen[0] == sizeof(p1) && memcmp(k.pl[0], p1, sizeof(p1)) == 0, "A-MSDU p1");
	CHECK(k.plen[1] == sizeof(p2) && memcmp(k.pl[1], p2, sizeof(p2)) == 0, "A-MSDU p2");

	/* Truncated second subframe: first is delivered, then an error. */
	memset(&k, 0, sizeof(k));
	r = awdl_parse_data_msdus(q, off - 3, msdu_collect, &k);
	CHECK(r < 0 && k.n == 1, "truncated A-MSDU: r=%d msdus=%d", r, k.n);
	/* Subframe length larger than the frame. */
	uint8_t bad[600];
	memcpy(bad, q, off);
	bad[IEEE80211_HDR_LEN + 2 + 12] = 0x7f;
	memset(&k, 0, sizeof(k));
	CHECK(awdl_parse_data_msdus(bad, off, msdu_collect, &k) < 0 && k.n == 0,
	      "oversized subframe length accepted");
	/* Null data (subtype 4): no body, not an error. */
	uint8_t nul[24];
	memcpy(nul, f, 24);
	nul[0] = 0x48;
	CHECK(awdl_data_hdr_len(nul, sizeof(nul), NULL) == 0 &&
	      awdl_parse_data_msdus(nul, sizeof(nul), msdu_collect, &k) == 0, "null data");
	/* QoS data too short for its QoS control field. */
	uint8_t qs[25];
	memcpy(qs, q, 25);
	CHECK(awdl_data_hdr_len(qs, sizeof(qs), NULL) == 0, "short QoS header accepted");
}

static void test_reject_garbage(void) {
	uint8_t junk[64];
	memset(junk, 0xA5, sizeof(junk));
	awdl_peer_view_t pv;
	int r = awdl_parse_action(junk, sizeof(junk), &pv);
	CHECK(r == AWDL_ERR_NOT_AWDL, "garbage not rejected as NOT_AWDL: %d", r);

	CHECK(awdl_parse_action(junk, 8, &pv) == AWDL_ERR_SHORT, "short frame not SHORT");
}

/* awdl_action_tlv_end finds the real end of a frame inside a longer buffer, as
 * when a chip's reported length is off by the 4-byte FCS. */
static void test_action_tlv_end(void) {
	uint8_t self_mac[6] = {0x02, 0xaa, 0xbb, 0xcc, 0xdd, 0xee};
	awdl_self_t self;
	awdl_self_init(&self, self_mac, "espdrop-c5", 6);
	uint8_t buf[1600];
	int n = awdl_build_action(buf, sizeof(buf), &self, AWDL_SUBTYPE_MIF, 0, AWDL_EAW_TU, 0);
	CHECK(n > 0, "build failed: %d", n);
	CHECK(awdl_action_tlv_end(buf, (size_t)n) == (size_t)n, "exact length");
	/* stale buffer bytes after the frame, as seen on the C5 (00 00 d4 36 ...) */
	const uint8_t tail[8] = {0x00, 0x00, 0xd4, 0x36, 0x82, 0x40, 0xd4, 0x36};
	memcpy(buf + n, tail, sizeof(tail));
	CHECK(awdl_action_tlv_end(buf, (size_t)n + 8) == (size_t)n, "end found despite tail");
	/* cut 4 bytes off: the last TLV no longer fits, so the walk stops before */
	size_t cut = awdl_action_tlv_end(buf, (size_t)n - 4);
	CHECK(cut < (size_t)n - 4 && cut > IEEE80211_HDR_LEN + 16, "cut frame: %zu", cut);
	uint8_t junk[64];
	memset(junk, 0xA5, sizeof(junk));
	CHECK(awdl_action_tlv_end(junk, sizeof(junk)) == 0, "garbage is no action frame");
}

static void test_sync_election_parse(void) {
	uint8_t self_mac[6] = {0x02, 0xaa, 0xbb, 0xcc, 0xdd, 0xee};
	awdl_self_t self;
	awdl_self_init(&self, self_mac, "espdrop-c3", 6);

	uint8_t buf[1500];
	int n = awdl_build_action(buf, sizeof(buf), &self, AWDL_SUBTYPE_MIF, 0 /* now_us */, AWDL_EAW_TU, 0 /* tx_down, aw_counter */);
	CHECK(n > 0, "build failed: %d", n);

	awdl_peer_view_t pv;
	CHECK(awdl_parse_action(buf, (size_t)n, &pv) == AWDL_OK, "parse");

	/* now_us=0: next AW is a full EAW away (64 TU), AW counter 0 */
	CHECK(pv.has_sync, "no sync params parsed");
	CHECK(pv.sync_tx_down_tu == AWDL_EAW_TU, "tx_down %u != %u", pv.sync_tx_down_tu, AWDL_EAW_TU);
	CHECK(pv.sync_aw_counter == 0, "aw_counter %u", pv.sync_aw_counter);

	/* election: we are our own master with OWL's init metric, counter 0 */
	CHECK(pv.has_election, "no election parsed");
	CHECK(memcmp(pv.master_addr, self_mac, 6) == 0, "master_addr mismatch");
	CHECK(pv.el_master_metric == AWDL_ELECTION_METRIC_INIT, "master_metric %u", pv.el_master_metric);
	CHECK(pv.el_master_counter == 0, "master_counter %u", pv.el_master_counter);
	CHECK(pv.el_height == 0, "height %u", pv.el_height);
}

static void test_sync_math(void) {
	awdl_sync_t s;
	awdl_sync_init(&s, 0);

	const uint64_t T = 5 * AWDL_TU_US + 123; /* some arbitrary non-aligned time */

	/* A perfectly in-phase master announcement yields zero error. */
	uint16_t ttna = awdl_sync_next_aw_tu(T, &s);
	uint16_t awc  = awdl_sync_current_aw(T, &s);
	CHECK(awdl_sync_error_tu(T, ttna, awc, &s) == 0, "in-phase error not 0");

	/* A master 3 TU ahead gives error -3. */
	CHECK(awdl_sync_error_tu(T, (uint16_t)(ttna + 3), awc, &s) == -3,
	      "off-phase error wrong: %lld",
	      (long long)awdl_sync_error_tu(T, (uint16_t)(ttna + 3), awc, &s));

	/* update() adopts the announced phase exactly (TU<->us is exact). */
	uint16_t target = 20;
	awdl_sync_update(T, target, awc, &s);
	CHECK(awdl_sync_next_aw_tu(T, &s) == target,
	      "update did not adopt phase: %u != %u", awdl_sync_next_aw_tu(T, &s), target);
}

static void set_peer_election(awdl_peer_t *p, uint32_t counter, uint32_t metric) {
	p->election.master_counter = counter;
	p->election.master_metric = metric;
	memcpy(p->election.master_addr, p->addr, 6); /* peer advertises itself as top master */
	p->election.height = 0;
}

static void test_election_run(void) {
	uint8_t me[6]     = {0x02, 0x00, 0x00, 0x00, 0x00, 0x01};
	uint8_t strong[6] = {0x02, 0x00, 0x00, 0x00, 0x00, 0x99};
	uint8_t weak[6]   = {0x02, 0x00, 0x00, 0x00, 0x00, 0x02};

	awdl_election_t self;
	awdl_election_init(&self, me); /* self_metric = 60, counter 0 */

	awdl_peer_table_t t;
	awdl_peers_init(&t);
	bool created;

	/* A weaker peer (metric 10 < our 60) must not win. */
	awdl_peer_t *pw = awdl_peers_add(&t, weak, 1000, &created);
	set_peer_election(pw, 0, 10);
	awdl_election_run(&self, &t);
	CHECK(awdl_election_is_sync_master(&self, me), "weak peer wrongly won");
	CHECK(self.height == 0, "height should stay 0, got %u", self.height);

	/* A stronger peer (metric 1000 > 60) must become our master. */
	awdl_peer_t *ps = awdl_peers_add(&t, strong, 1000, &created);
	set_peer_election(ps, 0, 1000);
	awdl_election_run(&self, &t);
	CHECK(awdl_election_is_sync_master(&self, strong), "strong peer did not win");
	CHECK(self.height == 1, "height should be 1, got %u", self.height);
	CHECK(self.master_metric == 1000, "master_metric %u", self.master_metric);

	/* Ageing with a cutoff past both peers empties the table. */
	CHECK(awdl_peers_count(&t) == 2, "count %d", awdl_peers_count(&t));
	awdl_peers_age(&t, 2000);
	CHECK(awdl_peers_count(&t) == 0, "ageing did not clear table");
}

static void test_builder_respects_cap(void) {
	/* A too-small cap must make awdl_build_action fail without writing past it.
	 * cap=40 lands exactly at a TLV start (24-byte MAC + 16-byte action header),
	 * which is the case that used to overrun via the length back-patch. */
	uint8_t self_mac[6] = {0x02, 1, 2, 3, 4, 5};
	awdl_self_t self;
	awdl_self_init(&self, self_mac, "x", 6);

	uint8_t buf[80];
	memset(buf, 0xAB, sizeof(buf));
	for (size_t cap = 24; cap <= 48; cap++) {
		int r = awdl_build_action(buf, cap, &self, AWDL_SUBTYPE_MIF, 0, 64, 0);
		CHECK(r == AWDL_ERR_ARG, "cap=%zu should fail, got %d", cap, r);
		/* nothing beyond cap may be touched */
		for (size_t i = cap; i < sizeof(buf); i++)
			CHECK(buf[i] == 0xAB, "cap=%zu wrote past end at byte %zu (0x%02x)",
			      cap, i, buf[i]);
		memset(buf, 0xAB, sizeof(buf)); /* restore canary */
	}
}

static void test_truncated_v2_no_clobber(void) {
	/* Hand-build an action frame with a well-formed v1 election TLV followed by
	 * a LENGTH-TRUNCATED v2 TLV (28 <= l < 40). The v2 must be rejected whole;
	 * the v1 master_addr must survive, and the record stays consistent. */
	uint8_t f[128];
	size_t o = 0;
	memset(f, 0, sizeof(f));
	/* 802.11 header: FC=action, addr2 = sender */
	f[0] = 0xd0; f[1] = 0x00;
	uint8_t sender[6] = {0x02, 0xAA, 0xAA, 0xAA, 0xAA, 0xAA};
	memcpy(f + 10, sender, 6);
	memcpy(f + 16, AWDL_BSSID, 6);
	o = 24;
	/* AWDL action header */
	f[o++] = 127; f[o++] = AWDL_OUI[0]; f[o++] = AWDL_OUI[1]; f[o++] = AWDL_OUI[2];
	f[o++] = 8; f[o++] = 0x10; f[o++] = AWDL_SUBTYPE_MIF; f[o++] = 0;
	for (int i = 0; i < 8; i++) f[o++] = 0; /* phy_tx, target_tx */

	/* v1 election TLV (type 5), 21 value bytes; master_addr = V1MA at value off 5 */
	uint8_t v1ma[6] = {0x02, 0x11, 0x11, 0x11, 0x11, 0x11};
	f[o++] = AWDL_TLV_ELECTION_PARAMS; f[o++] = 21; f[o++] = 0;
	size_t v1 = o;
	for (int i = 0; i < 21; i++) f[o++] = 0;
	memcpy(f + v1 + 5, v1ma, 6);       /* top_master_addr */
	f[v1 + 11] = 0x2a;                 /* some metric LSB, != 0 */

	/* v2 election TLV (type 24) but declared length 30 (< 40): reads at off 36 fail.
	 * Put a different master_addr (V2MA) at value off 0 to detect a clobber. */
	uint8_t v2ma[6] = {0x02, 0x22, 0x22, 0x22, 0x22, 0x22};
	f[o++] = AWDL_TLV_ELECTION_PARAMS_V2; f[o++] = 30; f[o++] = 0;
	size_t v2 = o;
	for (int i = 0; i < 30; i++) f[o++] = 0;
	memcpy(f + v2 + 0, v2ma, 6);

	awdl_peer_view_t pv;
	CHECK(awdl_parse_action(f, o, &pv) == AWDL_OK, "parse crafted frame");
	CHECK(pv.has_election, "v1 election should have been parsed");
	CHECK(memcmp(pv.master_addr, v1ma, 6) == 0,
	      "truncated v2 clobbered the v1 master_addr");
	CHECK(pv.el_self_counter == 0, "self_counter should remain v1's 0");
}

/* Craft what a real iPhone's action frame parses to, with a clock `iph` so the
 * announced timing is self-consistent and a strong election so it wins. */
static void iphone_announce(awdl_sync_t *iph, uint64_t t, const uint8_t ip[6],
                            awdl_peer_view_t *pv) {
	memset(pv, 0, sizeof(*pv));
	memcpy(pv->src_addr, ip, 6);
	pv->subtype = AWDL_SUBTYPE_MIF;
	pv->has_name = true;
	snprintf(pv->name, sizeof(pv->name), "iPhone");
	pv->has_version = true;
	pv->version = 0x3f;
	pv->devclass = AWDL_DEVCLASS_IOS;
	pv->has_chanseq = true;
	pv->chanseq_enc = AWDL_CHAN_ENC_OPCLASS;
	pv->chanseq[0] = 6;
	pv->chanseq[2] = 44; /* a 5 GHz slot the C3 cannot reach */
	pv->chanseq[8] = 6;
	pv->chanseq[10] = 44;
	pv->has_election = true;
	memcpy(pv->master_addr, ip, 6); /* its own top master */
	pv->el_height = 0;
	pv->el_master_metric = 1000;
	pv->el_master_counter = 7;
	pv->el_self_metric = 1000;
	pv->el_self_counter = 7;
	pv->has_sync = true;
	pv->sync_tx_down_tu = awdl_sync_next_aw_tu(t, iph);
	pv->sync_aw_counter = awdl_sync_current_aw(t, iph);
}

static void test_node_sync_hold(void) {
	uint8_t me[6] = {0x02, 0, 0, 0, 0, 0x01};
	uint8_t ip[6] = {0xca, 0x2a, 0xd6, 0x10, 0x8e, 0xae};

	awdl_node_t n;
	awdl_node_init(&n, me, "espdrop-c3", 6, 0);

	/* The iPhone has its own AW clock, offset from ours. */
	awdl_sync_t iph;
	awdl_sync_init(&iph, 7000);

	awdl_peer_view_t pv;
	uint64_t t1 = 100000; /* 100 ms */
	iphone_announce(&iph, t1, ip, &pv);
	awdl_node_on_rx_action(&n, &pv, -40, t1);

	awdl_node_status_t st;
	awdl_node_status(&n, &st);
	CHECK(st.have_master, "no master adopted");
	CHECK(memcmp(st.master_addr, ip, 6) == 0, "master is not the iPhone");
	CHECK(st.height == 1, "height %u != 1", st.height);
	CHECK(st.peer_count == 1, "peer_count %d", st.peer_count);
	/* First lock reports 0, not the (huge) gap between two unrelated clocks. */
	CHECK(st.last_sync_err_tu == 0, "first lock should report 0, got %lld",
	      (long long)st.last_sync_err_tu);

	/* After adopting the iPhone's timing, a later consistent announcement must
	 * measure a small residual error. */
	uint64_t t2 = t1 + 300000;
	iphone_announce(&iph, t2, ip, &pv);
	awdl_node_on_rx_action(&n, &pv, -40, t2);
	awdl_node_status(&n, &st);
	CHECK(st.last_sync_err_tu > -3 && st.last_sync_err_tu < 3,
	      "sync error should be ~0 after adopting, got %lld",
	      (long long)st.last_sync_err_tu);

	/* The peer is held within the timeout and aged out past it. */
	awdl_node_tick(&n, t2 + 1500000); /* +1.5 s, within 2 s */
	awdl_node_status(&n, &st);
	CHECK(st.peer_count == 1, "peer aged out too early");

	awdl_node_tick(&n, t2 + 3000000); /* +3 s past last update */
	awdl_node_status(&n, &st);
	CHECK(st.peer_count == 0, "peer should have aged out");
	CHECK(!st.have_master, "master should be gone after the peer aged out");
}

static void test_node_chanseq_mirror(void) {
	uint8_t me[6] = {0x02, 0, 0, 0, 0, 0x01};
	uint8_t ip[6] = {0xca, 0x2a, 0xd6, 0x10, 0x8e, 0xae};

	awdl_node_t n;
	awdl_node_init(&n, me, "espdrop-c3", 6, 0);

	awdl_sync_t iph;
	awdl_sync_init(&iph, 0);
	awdl_peer_view_t pv;
	iphone_announce(&iph, 100000, ip, &pv); /* chanseq: 6 at 0/8, 44 at 2/10 */
	awdl_node_on_rx_action(&n, &pv, -40, 100000);

	/* While the master is present, status names it and reports its sequence
	 * (what the `node` command prints as mseq=). */
	awdl_node_status_t st;
	awdl_node_status(&n, &st);
	CHECK(st.have_master && memcmp(st.master_addr, ip, 6) == 0, "status: master");
	CHECK(st.master_has_chanseq, "status: master chanseq not reported");
	for (int i = 0; i < (int)AWDL_CHANSEQ_LEN; i++)
		CHECK(st.master_chanseq[i] == pv.chanseq[i], "status mseq[%d]=%u expected %u",
		      i, st.master_chanseq[i], pv.chanseq[i]);
	CHECK(st.seq_derived, "status: mirror not derived");

	/* What we now advertise must be channel 6 exactly in the master's channel-6
	 * slots (0 and 8) and absent (0) everywhere else - never a flat all-6. */
	uint8_t buf[1500];
	int len = awdl_node_build_action(&n, buf, sizeof(buf), AWDL_SUBTYPE_MIF,
	                                 150000);
	CHECK(len > 0, "build our MIF: %d", len);
	awdl_peer_view_t ours;
	CHECK(awdl_parse_action(buf, (size_t)len, &ours) == AWDL_OK, "parse our MIF");
	CHECK(ours.has_chanseq, "our MIF carries no chanseq");
	for (int i = 0; i < (int)AWDL_CHANSEQ_LEN; i++) {
		uint8_t expect = (i == 0 || i == 8) ? 6 : 0;
		CHECK(ours.chanseq[i] == expect, "our chanseq[%d]=%u expected %u",
		      i, ours.chanseq[i], expect);
	}

	/* After the master ages out, KEEP the last mirror: falling back to a flat
	 * all-6 sequence is the shape iOS 26 refuses to send unicast to. */
	awdl_node_tick(&n, 100000 + 3000000);
	CHECK(!n.have_master, "master should have aged out");
	len = awdl_node_build_action(&n, buf, sizeof(buf), AWDL_SUBTYPE_MIF, 3200000);
	CHECK(awdl_parse_action(buf, (size_t)len, &ours) == AWDL_OK, "parse MIF 2");
	for (int i = 0; i < (int)AWDL_CHANSEQ_LEN; i++) {
		uint8_t expect = (i == 0 || i == 8) ? 6 : 0;
		CHECK(ours.chanseq[i] == expect, "kept chanseq[%d]=%u expected %u",
		      i, ours.chanseq[i], expect);
	}
	awdl_node_status(&n, &st);
	CHECK(st.seq_derived && !st.have_master && !st.master_has_chanseq,
	      "status after age-out: derived=%d master=%d mseq=%d",
	      st.seq_derived, st.have_master, st.master_has_chanseq);
	CHECK(st.self_chanseq[8] == 6 && st.self_chanseq[2] == 0, "status self seq");
}

/* Before any master: static sequence. A master that names ch6 nowhere must not
 * replace a good mirror, and must not produce a flat sequence either. */
static void test_node_chanseq_keep_on_no_ch6(void) {
	uint8_t me[6] = {0x02, 0, 0, 0, 0, 0x01};
	uint8_t ip[6] = {0xca, 0x2a, 0xd6, 0x10, 0x8e, 0xae};
	awdl_node_t n;
	awdl_node_init(&n, me, "espdrop-c3", 6, 0);
	CHECK(!n.seq_derived, "fresh node must not be derived");
	for (int i = 0; i < (int)AWDL_CHANSEQ_LEN; i++)
		CHECK(n.self.seq[i][0] == 6, "boot static slot %d = %u", i, n.self.seq[i][0]);

	awdl_sync_t iph;
	awdl_sync_init(&iph, 0);
	awdl_peer_view_t pv;
	iphone_announce(&iph, 100000, ip, &pv);          /* 6 at slots 0 and 8 */
	awdl_node_on_rx_action(&n, &pv, -40, 100000);
	CHECK(n.seq_derived, "mirror not derived");

	/* The same master now advertises no channel-6 slot at all. */
	iphone_announce(&iph, 200000, ip, &pv);
	memset(pv.chanseq, 0, sizeof(pv.chanseq));
	pv.chanseq[2] = 44; pv.chanseq[10] = 149;
	awdl_node_on_rx_action(&n, &pv, -40, 200000);
	for (int i = 0; i < (int)AWDL_CHANSEQ_LEN; i++) {
		uint8_t expect = (i == 0 || i == 8) ? 6 : 0;
		CHECK(n.self.seq[i][0] == expect, "no-ch6 master: slot %d = %u expected %u",
		      i, n.self.seq[i][0], expect);
	}

	/* And when it comes back with a different ch6 slot, follow it. */
	iphone_announce(&iph, 300000, ip, &pv);
	memset(pv.chanseq, 0, sizeof(pv.chanseq));
	pv.chanseq[4] = 6; pv.chanseq[12] = 149;
	awdl_node_on_rx_action(&n, &pv, -40, 300000);
	for (int i = 0; i < (int)AWDL_CHANSEQ_LEN; i++) {
		uint8_t expect = (i == 4) ? 6 : 0;
		CHECK(n.self.seq[i][0] == expect, "re-mirror: slot %d = %u expected %u",
		      i, n.self.seq[i][0], expect);
	}
}

static void test_chanseq_empty_intersection(void) {
	uint8_t me[6] = {0x02, 0, 0, 0, 0, 0x01};
	awdl_self_t self;
	awdl_self_init(&self, me, "x", 6);
	/* Start from a mirror rather than the static all-6 of init, so "left
	 * untouched" cannot be confused with a fallback to the static sequence. */
	uint8_t good[AWDL_CHANSEQ_LEN] = {0};
	good[4] = 6; good[12] = 149;
	CHECK(awdl_self_set_chanseq_intersect(&self, good, 6), "mirror must succeed");
	/* master with no channel-6 slot at all: report it and leave our sequence
	 * untouched, never "present nowhere". */
	uint8_t mseq[AWDL_CHANSEQ_LEN] = {0};
	mseq[2] = 44; mseq[10] = 149;
	CHECK(!awdl_self_set_chanseq_intersect(&self, mseq, 6),
	      "empty intersection must return false");
	for (int i = 0; i < (int)AWDL_CHANSEQ_LEN; i++) {
		uint8_t exp = (i == 4) ? 6 : 0;
		CHECK(self.seq[i][0] == exp, "empty-intersection left slot %d = %u expected %u",
		      i, self.seq[i][0], exp);
	}
}

static void test_chanseq_realistic_slot0(void) {
	/* A realistic idle iPhone sequence: slot 0 is a 5 GHz infra channel, ch6
	 * appears only at slot 8. We advertise 6 at slot 8 and absent (0) elsewhere,
	 * slot 0 included. This documents the actual behaviour; whether iOS accepts
	 * slot-0-absent is an open hardware question (see awdl_frame.h). */
	uint8_t me[6] = {0x02, 0, 0, 0, 0, 0x01};
	awdl_self_t self;
	awdl_self_init(&self, me, "x", 6);
	uint8_t mseq[AWDL_CHANSEQ_LEN] = {0};
	mseq[0] = 132; mseq[2] = 149; mseq[8] = 6; mseq[10] = 149;
	awdl_self_set_chanseq_intersect(&self, mseq, 6);
	for (int i = 0; i < (int)AWDL_CHANSEQ_LEN; i++) {
		uint8_t exp = (i == 8) ? 6 : 0;
		CHECK(self.seq[i][0] == exp, "slot0-infra case slot %d = %u expected %u",
		      i, self.seq[i][0], exp);
	}
}

static void test_tx_slot_gating(void) {
	uint8_t me[6] = {0x02, 0, 0, 0, 0, 0x01};
	awdl_node_t n;
	awdl_node_init(&n, me, "x", 6, 0);

	/* Alone (static all-6): every slot is present, so TX is always ready. */
	uint64_t eaw_us = (uint64_t)AWDL_EAW_TU * AWDL_TU_US;
	for (int s = 0; s < (int)AWDL_CHANSEQ_LEN; s++)
		CHECK(awdl_node_tx_slot_ready(&n, (uint64_t)s * eaw_us + 1000),
		      "static: slot %d not ready", s);

	/* Sparse (ch6 only in slots 0 and 8): ready only in those slots. */
	uint8_t mseq[AWDL_CHANSEQ_LEN] = {0};
	mseq[0] = 6; mseq[8] = 6;
	awdl_self_set_chanseq_intersect(&n.self, mseq, 6);
	awdl_sync_init(&n.sync, 0);
	for (int s = 0; s < (int)AWDL_CHANSEQ_LEN; s++) {
		bool present = (s == 0 || s == 8);
		bool ready = awdl_node_tx_slot_ready(&n, (uint64_t)s * eaw_us + 1000);
		CHECK(ready == present, "sparse: slot %d ready=%d expected %d",
		      s, ready, present);
	}
}

int main(void) {
	test_mif_roundtrip();
	test_psf_roundtrip();
	test_data_roundtrip();
	test_data_qos_and_flags();
	test_dedup();
	test_data_amsdu();
	test_reject_garbage();
	test_action_tlv_end();
	test_sync_election_parse();
	test_sync_math();
	test_election_run();
	test_builder_respects_cap();
	test_truncated_v2_no_clobber();
	test_node_sync_hold();
	test_node_chanseq_mirror();
	test_chanseq_empty_intersection();
	test_node_chanseq_keep_on_no_ch6();
	test_chanseq_realistic_slot0();
	test_tx_slot_gating();

	if (g_fail == 0) printf("\nALL TESTS PASSED\n");
	else printf("\n%d CHECK(S) FAILED\n", g_fail);
	return g_fail ? 1 : 0;
}
