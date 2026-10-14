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
	int n = awdl_build_action(buf, sizeof(buf), &self, AWDL_SUBTYPE_MIF, 123456789ull);
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
	int n = awdl_build_action(buf, sizeof(buf), &self, AWDL_SUBTYPE_PSF, 0);
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
	int n = awdl_build_action(buf, sizeof(buf), &self, AWDL_SUBTYPE_MIF, 0 /* now_us */);
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

int main(void) {
	test_mif_roundtrip();
	test_psf_roundtrip();
	test_data_roundtrip();
	test_reject_garbage();
	test_sync_election_parse();
	test_sync_math();
	test_election_run();

	if (g_fail == 0) printf("\nALL TESTS PASSED\n");
	else printf("\n%d CHECK(S) FAILED\n", g_fail);
	return g_fail ? 1 : 0;
}
