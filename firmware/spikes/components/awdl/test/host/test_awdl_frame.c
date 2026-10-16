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

static void test_reject_garbage(void) {
	uint8_t junk[64];
	memset(junk, 0xA5, sizeof(junk));
	awdl_peer_view_t pv;
	int r = awdl_parse_action(junk, sizeof(junk), &pv);
	CHECK(r == AWDL_ERR_NOT_AWDL, "garbage not rejected as NOT_AWDL: %d", r);

	CHECK(awdl_parse_action(junk, 8, &pv) == AWDL_ERR_SHORT, "short frame not SHORT");
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

	/* After the master ages out, fall back to the lone static all-6 sequence. */
	awdl_node_tick(&n, 100000 + 3000000);
	len = awdl_node_build_action(&n, buf, sizeof(buf), AWDL_SUBTYPE_MIF, 3200000);
	CHECK(awdl_parse_action(buf, (size_t)len, &ours) == AWDL_OK, "parse MIF 2");
	for (int i = 0; i < (int)AWDL_CHANSEQ_LEN; i++)
		CHECK(ours.chanseq[i] == 6, "fallback chanseq[%d]=%u expected 6",
		      i, ours.chanseq[i]);
}

static void test_chanseq_empty_intersection(void) {
	uint8_t me[6] = {0x02, 0, 0, 0, 0, 0x01};
	awdl_self_t self;
	awdl_self_init(&self, me, "x", 6);
	/* master with no channel-6 slot at all: must fall back to static all-6,
	 * never advertise presence nowhere. */
	uint8_t mseq[AWDL_CHANSEQ_LEN] = {0};
	mseq[2] = 44; mseq[10] = 149;
	awdl_self_set_chanseq_intersect(&self, mseq, 6);
	for (int i = 0; i < (int)AWDL_CHANSEQ_LEN; i++)
		CHECK(self.seq[i][0] == 6, "empty-intersection fallback slot %d = %u",
		      i, self.seq[i][0]);
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
	test_reject_garbage();
	test_sync_election_parse();
	test_sync_math();
	test_election_run();
	test_builder_respects_cap();
	test_truncated_v2_no_clobber();
	test_node_sync_hold();
	test_node_chanseq_mirror();
	test_chanseq_empty_intersection();
	test_chanseq_realistic_slot0();
	test_tx_slot_gating();

	if (g_fail == 0) printf("\nALL TESTS PASSED\n");
	else printf("\n%d CHECK(S) FAILED\n", g_fail);
	return g_fail ? 1 : 0;
}
