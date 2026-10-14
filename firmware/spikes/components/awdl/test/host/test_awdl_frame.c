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

#include <stdio.h>
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

int main(void) {
	test_mif_roundtrip();
	test_psf_roundtrip();
	test_data_roundtrip();
	test_reject_garbage();

	if (g_fail == 0) printf("\nALL TESTS PASSED\n");
	else printf("\n%d CHECK(S) FAILED\n", g_fail);
	return g_fail ? 1 : 0;
}
