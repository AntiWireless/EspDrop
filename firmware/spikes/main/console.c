/*
 * EspDrop - serial console for the AWDL M0 hardware spikes.
 *
 * Commands (type `help` on the device):
 *   info                 show self MAC, channel, promiscuous state
 *   mac <aa:bb:..:ff>    override the station MAC (ACK experiment)
 *   chan <1..13>         set the 2.4 GHz channel
 *   rx on|off            enable/disable promiscuous AWDL receive
 *   tx psf|mif [n]       inject n (default 1) action frames
 *   txdata <hexpayload>  inject one AWDL data frame to the broadcast AWDL addr
 *   peers                drain and print discovered-peer events
 *   stats                print rx/tx counters
 *   reset                zero the counters
 *   chanlat [n]          measure esp_wifi_set_channel latency over n hops
 *   usbbench [kib]       stream kib*1024 bytes so the host can time USB CDC
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_console.h"
#include "esp_timer.h"
#include "esp_wifi.h"

#include "awdl_frame.h"
#include "awdl_app.h"
#include "radio.h"

static awdl_self_t s_self;

static void refresh_self(void) {
	uint8_t mac[6];
	radio_get_self_mac(mac);
	awdl_self_init(&s_self, mac, "espdrop-c3", radio_get_channel());
}

static int parse_mac(const char *s, uint8_t out[6]) {
	unsigned v[6];
	if (sscanf(s, "%x:%x:%x:%x:%x:%x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6)
		return -1;
	for (int i = 0; i < 6; i++) {
		if (v[i] > 0xff) return -1;
		out[i] = (uint8_t)v[i];
	}
	return 0;
}

static int cmd_info(int argc, char **argv) {
	(void)argc; (void)argv;
	uint8_t mac[6];
	radio_get_self_mac(mac);
	printf("self MAC : %02x:%02x:%02x:%02x:%02x:%02x\n",
	       mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
	printf("channel  : %u\n", radio_get_channel());
	printf("promisc  : %s\n", radio_promisc_enabled() ? "on" : "off");
	printf("awdl bssid: %02x:%02x:%02x:%02x:%02x:%02x\n",
	       AWDL_BSSID[0], AWDL_BSSID[1], AWDL_BSSID[2],
	       AWDL_BSSID[3], AWDL_BSSID[4], AWDL_BSSID[5]);
	return 0;
}

static int cmd_mac(int argc, char **argv) {
	if (argc != 2) { printf("usage: mac <aa:bb:cc:dd:ee:ff>\n"); return 1; }
	uint8_t mac[6];
	if (parse_mac(argv[1], mac) != 0) { printf("bad MAC\n"); return 1; }
	int err = radio_set_self_mac(mac);
	if (err) { printf("set_mac failed: 0x%x\n", err); return 1; }
	refresh_self();
	awdl_app_set_self(mac, radio_get_channel()); /* rebuild node (resets sync) */
	printf("MAC set\n");
	return 0;
}

static int cmd_chan(int argc, char **argv) {
	if (argc != 2) { printf("usage: chan <1..13>\n"); return 1; }
	int ch = atoi(argv[1]);
	if (ch < 1 || ch > 13) { printf("channel out of range\n"); return 1; }
	int err = radio_set_channel((uint8_t)ch);
	if (err) { printf("set_channel failed: 0x%x\n", err); return 1; }
	refresh_self();
	uint8_t mac[6];
	radio_get_self_mac(mac);
	awdl_app_set_self(mac, (uint8_t)ch); /* rebuild node (resets sync) */
	printf("channel %d\n", ch);
	return 0;
}

static int cmd_rx(int argc, char **argv) {
	if (argc != 2) { printf("usage: rx on|off\n"); return 1; }
	bool on = strcmp(argv[1], "on") == 0;
	int err = radio_promisc(on);
	if (err) { printf("promisc failed: 0x%x\n", err); return 1; }
	printf("rx %s\n", on ? "on" : "off");
	return 0;
}

static int cmd_tx(int argc, char **argv) {
	if (argc < 2) { printf("usage: tx psf|mif [count]\n"); return 1; }
	uint8_t subtype;
	if (strcmp(argv[1], "psf") == 0) subtype = AWDL_SUBTYPE_PSF;
	else if (strcmp(argv[1], "mif") == 0) subtype = AWDL_SUBTYPE_MIF;
	else { printf("unknown frame '%s'\n", argv[1]); return 1; }

	int count = (argc >= 3) ? atoi(argv[2]) : 1;
	if (count < 1) count = 1;

	/* Built with the node's synchronised AW clock. Results show up in `stats`. */
	for (int i = 0; i < count; i++)
		awdl_app_tx(subtype);
	printf("tx %s x%d requested (see `stats` for tx counters)\n", argv[1], count);
	return 0;
}

static int hexnib(char c) {
	if (c >= '0' && c <= '9') return c - '0';
	if (c >= 'a' && c <= 'f') return c - 'a' + 10;
	if (c >= 'A' && c <= 'F') return c - 'A' + 10;
	return -1;
}

static int cmd_txdata(int argc, char **argv) {
	if (argc != 2) { printf("usage: txdata <hexpayload>\n"); return 1; }
	const char *h = argv[1];
	size_t hlen = strlen(h);
	if (hlen % 2) { printf("hex length must be even\n"); return 1; }
	static uint8_t payload[1400];
	size_t plen = hlen / 2;
	if (plen > sizeof(payload)) { printf("payload too long\n"); return 1; }
	for (size_t i = 0; i < plen; i++) {
		int hi = hexnib(h[2 * i]), lo = hexnib(h[2 * i + 1]);
		if (hi < 0 || lo < 0) { printf("bad hex\n"); return 1; }
		payload[i] = (uint8_t)((hi << 4) | lo);
	}
	uint8_t buf[1500];
	int n = awdl_build_data(buf, sizeof(buf), &s_self, AWDL_BROADCAST, payload, plen);
	if (n <= 0) { printf("build failed: %d\n", n); return 1; }
	int err = radio_tx(buf, n);
	printf("txdata len=%d: %s\n", n, err == 0 ? "ok" : "err");
	return 0;
}

static int cmd_peers(int argc, char **argv) {
	(void)argc; (void)argv;
	static char buf[1024];
	int n = awdl_app_format_peers(buf, sizeof(buf));
	if (n <= 0) printf("(no peers)\n");
	else        printf("%s", buf);
	return 0;
}

static int cmd_sync(int argc, char **argv) {
	(void)argc; (void)argv;
	awdl_node_status_t st;
	if (!awdl_app_status(&st)) { printf("awdl not started\n"); return 1; }
	if (st.have_master)
		printf("master %02x:%02x:%02x:%02x:%02x:%02x  height %" PRIu32 "  "
		       "sync_err %lld TU  peers %d\n",
		       st.master_addr[0], st.master_addr[1], st.master_addr[2],
		       st.master_addr[3], st.master_addr[4], st.master_addr[5],
		       st.height, (long long)st.last_sync_err_tu, st.peer_count);
	else
		printf("no master (we are our own master)  peers %d\n", st.peer_count);
	return 0;
}

static int cmd_stats(int argc, char **argv) {
	(void)argc; (void)argv;
	radio_stats_t s;
	radio_get_stats(&s);
	printf("rx: action=%llu data=%llu other_bssid=%llu parse_err=%llu\n",
	       s.rx_action, s.rx_data, s.rx_other_bssid, s.rx_parse_err);
	printf("tx: ok=%llu err=%llu  tx_done: ok=%llu fail=%llu\n",
	       s.tx_ok, s.tx_err, s.tx_done_ok, s.tx_done_fail);
	return 0;
}

static int cmd_reset(int argc, char **argv) {
	(void)argc; (void)argv;
	radio_reset_stats();
	printf("stats reset\n");
	return 0;
}

static int cmd_chanlat(int argc, char **argv) {
	int n = (argc >= 2) ? atoi(argv[1]) : 100;
	if (n < 1) n = 1;
	const uint8_t chans[] = {1, 6, 11};
	int64_t total = 0, worst = 0;
	for (int i = 0; i < n; i++) {
		uint8_t ch = chans[i % 3];
		int64_t t0 = esp_timer_get_time();
		esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
		int64_t dt = esp_timer_get_time() - t0;
		total += dt;
		if (dt > worst) worst = dt;
	}
	radio_set_channel(radio_get_channel());
	printf("chanlat over %d hops: mean %lld us, worst %lld us\n",
	       n, (long long)(total / n), (long long)worst);
	return 0;
}

static int cmd_usbbench(int argc, char **argv) {
	int kib = (argc >= 2) ? atoi(argv[1]) : 256;
	if (kib < 1) kib = 1;
	static char line[64];
	memset(line, 'A', sizeof(line) - 1);
	line[sizeof(line) - 1] = '\n';
	int64_t t0 = esp_timer_get_time();
	long total = (long)kib * 1024;
	long written = 0;
	while (written < total) {
		fwrite(line, 1, sizeof(line), stdout);
		written += sizeof(line);
	}
	fflush(stdout);
	int64_t dt = esp_timer_get_time() - t0;
	/* device-side timing is a rough lower bound (TinyUSB buffers); time it on
	 * the host too, e.g. `time cat /dev/ttyACM0 > /dev/null` against this run. */
	printf("\nusbbench: %ld bytes in %lld us (device-side) = %.1f kB/s\n",
	       written, (long long)dt, dt ? written * 1000.0 / (double)dt : 0.0);
	return 0;
}

static void reg(const char *cmd, const char *help, esp_console_cmd_func_t fn) {
	esp_console_cmd_t c = { .command = cmd, .help = help, .hint = NULL, .func = fn };
	esp_console_cmd_register(&c);
}

void console_register_commands(void) {
	refresh_self();
	reg("info",     "show self MAC, channel, promiscuous state", cmd_info);
	reg("mac",      "mac <aa:bb:cc:dd:ee:ff> - override station MAC", cmd_mac);
	reg("chan",     "chan <1..13> - set 2.4 GHz channel", cmd_chan);
	reg("rx",       "rx on|off - promiscuous AWDL receive", cmd_rx);
	reg("tx",       "tx psf|mif [count] - inject action frame(s)", cmd_tx);
	reg("txdata",   "txdata <hexpayload> - inject one AWDL data frame", cmd_txdata);
	reg("peers",    "print the current peer table (name, age, channel seq)", cmd_peers);
	reg("sync",     "print master, sync-tree height, sync error, peer count", cmd_sync);
	reg("stats",    "print rx/tx counters", cmd_stats);
	reg("reset",    "zero the counters", cmd_reset);
	reg("chanlat",  "chanlat [n] - measure channel-switch latency", cmd_chanlat);
	reg("usbbench", "usbbench [kib] - stream data for host-side USB timing", cmd_usbbench);
}
