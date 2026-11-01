/*
 * EspDrop - serial console for the AWDL hardware spikes (ESP32-C3 and -C5).
 *
 * Commands (type `help` on the device):
 *   info                 chip, MAC, channel, country, band mode, TX power
 *   mac <aa:bb:..:ff>    override the station MAC (ACK experiment)
 *   chan <n>             set the channel (1..13; on the C5 also 36..48, 149..173)
 *   rx on|off            enable/disable promiscuous AWDL receive
 *   tx psf|mif [n]       inject n (default 1) action frames
 *   txdata <hexpayload>  inject one AWDL data frame to the broadcast AWDL addr
 *   peers                drain and print discovered-peer events
 *   stats                print rx/tx counters
 *   reset                zero the counters
 *   chanlat [n] [c1,c2,..] time esp_wifi_set_channel over n hops through a list
 *   txpwr [q]            show / set the TX power ceiling (0.25 dBm units)
 *   usbbench [kib]       stream kib*1024 bytes so the host can time USB CDC
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_chip_info.h"
#include "esp_console.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "sdkconfig.h"
#include "soc/soc_caps.h"

#include "awdl_frame.h"
#include "awdl_app.h"
#include "radio.h"

static awdl_self_t s_self;

static void refresh_self(void) {
	uint8_t mac[6];
	radio_get_self_mac(mac);
	awdl_self_init(&s_self, mac, CONFIG_ESPDROP_AWDL_NAME, radio_get_channel());
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

#if SOC_WIFI_SUPPORT_5G
#define CHAN_RANGE "1..13 | 36..48 | 149..173"
#else
#define CHAN_RANGE "1..13"
#endif

static int cmd_info(int argc, char **argv) {
	(void)argc; (void)argv;
	esp_chip_info_t ci;
	esp_chip_info(&ci);
	printf("chip     : %s rev v%u.%u, CPU %d MHz\n", CONFIG_IDF_TARGET,
	       (unsigned)(ci.revision / 100), (unsigned)(ci.revision % 100),
	       CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ);
	uint8_t mac[6];
	radio_get_self_mac(mac);
	printf("self MAC : %02x:%02x:%02x:%02x:%02x:%02x\n",
	       mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
	printf("channel  : %u\n", radio_get_channel());
	char cc[4] = {0};
	if (esp_wifi_get_country_code(cc) == ESP_OK) printf("country  : %s\n", cc);
#if SOC_WIFI_SUPPORT_5G
	wifi_band_mode_t bm;
	if (esp_wifi_get_band_mode(&bm) == ESP_OK)
		printf("band mode: %s\n", bm == WIFI_BAND_MODE_AUTO ? "2.4 + 5 GHz"
		                         : bm == WIFI_BAND_MODE_5G_ONLY ? "5 GHz only" : "2.4 GHz only");
#endif
	int8_t q = radio_get_max_tx_power();
	printf("tx power : max %.2f dBm (%d)\n", q / 4.0, q);
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
	if (argc != 2) { printf("usage: chan <" CHAN_RANGE ">\n"); return 1; }
	int ch = atoi(argv[1]);
	if (ch < 1 || ch > 255 || !radio_channel_supported((uint8_t)ch)) {
		printf("channel %d not supported here (" CHAN_RANGE ")\n", ch);
		return 1;
	}
	int err = radio_set_channel((uint8_t)ch);
	if (err) { printf("set_channel failed: %s\n", esp_err_to_name(err)); return 1; }
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
	printf("rx data: msdu=%llu amsdu=%llu dup=%llu data_perr=%llu\n",
	       s.rx_msdu, s.rx_amsdu, s.rx_dup, s.rx_data_perr);
	printf("rx frame end: at sig_len-4=%llu at sig_len=%llu at dump_len=%llu other=%llu\n",
	       s.rx_end_sig4, s.rx_end_sig, s.rx_end_dump, s.rx_end_other);
	printf("rx checks: fcs_ok=%llu fcs_bad=%llu fcs_at_sig=%llu fcs_at_dump=%llu "
	       "type_mismatch=%llu ip6_short=%llu ip6_long=%llu\n",
	       s.rx_fcs_ok, s.rx_fcs_bad, s.rx_fcs_at_sig, s.rx_fcs_at_dump,
	       s.rx_type_mismatch, s.rx_ip6_short, s.rx_ip6_long);
	printf("tx: ok=%llu err=%llu  tx_done: ok=%llu fail=%llu  unicast data: ack=%llu noack=%llu\n",
	       s.tx_ok, s.tx_err, s.tx_done_ok, s.tx_done_fail, s.tx_uc_ack, s.tx_uc_noack);
	return 0;
}

/* Length fields and tail bytes of the last AWDL action frame (bring-up). */
static int cmd_rxprobe(int argc, char **argv) {
	(void)argc; (void)argv;
	radio_rx_probe_t p;
	radio_get_rx_probe(&p);
	if (!p.count) { printf("no AWDL action frame yet (rx on?)\n"); return 1; }
	printf("frames=%lu sig_len=%u dump_len=%u rx_state=%u\n", (unsigned long)p.count,
	       p.sig_len, p.dump_len, p.rx_state);
	printf("bytes [sig_len-8, sig_len+8):");
	for (int i = 0; i < 16; i++) printf("%s%02x", i == 8 ? " | " : " ", p.tail[i]);
	printf("\nTLVs end at %u (sig_len-4=%u, sig_len=%u, dump_len=%u)\n", p.tlv_end,
	       (unsigned)(p.sig_len - 4), p.sig_len, p.dump_len);
	printf("FCS found at: sig_len-4 %s, sig_len %s, dump_len %s\n",
	       p.fcs_at_sig_minus4 ? "yes" : "no", p.fcs_at_sig ? "yes" : "no",
	       p.fcs_at_dump ? "yes" : "no");
	return 0;
}

static int cmd_reset(int argc, char **argv) {
	(void)argc; (void)argv;
	radio_reset_stats();
	printf("stats reset\n");
	return 0;
}

#define CHANLAT_MAX   1000
#define CHANLAT_CHANS 8
#if SOC_WIFI_SUPPORT_5G
#define CHANLAT_DEFAULT "6,44,149"   /* both bands: what a dual-band AWDL node does */
#else
#define CHANLAT_DEFAULT "1,6,11"
#endif

static int cmp_i32(const void *a, const void *b) {
	int32_t x = *(const int32_t *)a, y = *(const int32_t *)b;
	return (x > y) - (x < y);
}

static void chanlat_report(const char *what, int32_t *v, int n) {
	if (n == 0) { printf("  %-10s no hops\n", what); return; }
	qsort(v, (size_t)n, sizeof(v[0]), cmp_i32);
	int64_t sum = 0;
	for (int i = 0; i < n; i++) sum += v[i];
	int p99 = (n * 99 + 99) / 100 - 1;   /* ceil(0.99 n) - 1 */
	printf("  %-10s %4d hops  mean %lld  min %ld  p50 %ld  p99 %ld  max %ld us\n",
	       what, n, (long long)(sum / n), (long)v[0], (long)v[n / 2],
	       (long)v[p99], (long)v[n - 1]);
}

/*
 * chanlat [n] [c1,c2,...]: n calls of esp_wifi_set_channel cycling through the
 * list, timed around the call (the radio may still settle afterwards). Hops
 * within a band and across bands are reported separately, the first band change
 * on its own (it may calibrate once). Our own AWDL TX is paused meanwhile, and
 * the power is capped if the list touches 149+.
 */
static int cmd_chanlat(int argc, char **argv) {
	int n = (argc >= 2) ? atoi(argv[1]) : 100;
	if (n < 1) n = 1;
	if (n > CHANLAT_MAX) n = CHANLAT_MAX;

	char list[64];
	strncpy(list, (argc >= 3) ? argv[2] : CHANLAT_DEFAULT, sizeof(list) - 1);
	list[sizeof(list) - 1] = '\0';
	uint8_t chans[CHANLAT_CHANS];
	int nch = 0;
	bool srd = false;
	for (char *tok = strtok(list, ","); tok; tok = strtok(NULL, ",")) {
		int ch = atoi(tok);
		if (nch == CHANLAT_CHANS || ch < 1 || ch > 255 ||
		    !radio_channel_supported((uint8_t)ch)) {
			printf("usage: chanlat [n] [c1,c2,...]  up to %d channels from " CHAN_RANGE "\n",
			       CHANLAT_CHANS);
			return 1;
		}
		chans[nch++] = (uint8_t)ch;
		if (ch >= 149) srd = true;
	}
	if (nch < 1) { printf("usage: chanlat [n] [c1,c2,...]\n"); return 1; }

	static int32_t same[CHANLAT_MAX], cross[CHANLAT_MAX];
	int ns = 0, nc = 0, errs = 0, wrong = 0;
	esp_err_t first_err = ESP_OK;
	int32_t first_dt = -1;
	uint8_t home = radio_get_channel(), prev = home, first_from = 0, first_to = 0;

	awdl_app_pause_tx(true);
	int8_t q = radio_get_max_tx_power();
	if (srd && q > 44) esp_wifi_set_max_tx_power(44);   /* restored with the channel */
	for (int i = 0; i < n; i++) {
		uint8_t ch = chans[i % nch];
		int64_t t0 = esp_timer_get_time();
		esp_err_t e = esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
		int32_t dt = (int32_t)(esp_timer_get_time() - t0);
		if (e != ESP_OK) {
			if (errs++ == 0) first_err = e;
			continue;
		}
		uint8_t pri = 0;
		wifi_second_chan_t sec;
		if (esp_wifi_get_channel(&pri, &sec) != ESP_OK || pri != ch) wrong++;
		bool xband = (ch > 14) != (prev > 14);
		if (xband && first_dt < 0) {
			first_dt = dt; first_from = prev; first_to = ch;   /* kept apart */
		} else if (xband) {
			cross[nc++] = dt;
		} else {
			same[ns++] = dt;
		}
		prev = ch;
	}
	int rerr = radio_set_channel(home);   /* also restores the TX power ceiling */
	awdl_app_pause_tx(false);

	printf("chanlat %d hops over %s (CPU %d MHz):\n", n,
	       (argc >= 3) ? argv[2] : CHANLAT_DEFAULT, CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ);
	if (first_dt >= 0)
		printf("  first band change %u -> %u: %ld us (not in the stats below)\n",
		       first_from, first_to, (long)first_dt);
	chanlat_report("same band", same, ns);
	chanlat_report("cross band", cross, nc);
	if (errs)  printf("  %d hops failed (first: %s)\n", errs, esp_err_to_name(first_err));
	if (wrong) printf("  %d hops reported another channel afterwards\n", wrong);
	if (rerr)  printf("  back to channel %u failed: %s\n", home, esp_err_to_name(rerr));
	return 0;
}

static int cmd_txpwr(int argc, char **argv) {
	if (argc >= 2) {
		int q = atoi(argv[1]);
		if (q < 8 || q > 84) { printf("usage: txpwr [8..84]  (0.25 dBm units, 80 = 20 dBm)\n"); return 1; }
		int err = radio_set_max_tx_power((int8_t)q);
		if (err) { printf("set failed: %s\n", esp_err_to_name(err)); return 1; }
	}
	uint8_t ch = radio_get_channel();
	int8_t q = radio_get_max_tx_power();
	printf("tx power ceiling %.2f dBm (%d) on channel %u%s\n", q / 4.0, q, ch,
	       ch >= 149 ? " (capped at 11 dBm on 149+)" : "");
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
	reg("info",     "show chip, MAC, channel, country, band mode, TX power", cmd_info);
	reg("mac",      "mac <aa:bb:cc:dd:ee:ff> - override station MAC", cmd_mac);
	reg("chan",     "chan <" CHAN_RANGE "> - set channel", cmd_chan);
	reg("rx",       "rx on|off - promiscuous AWDL receive", cmd_rx);
	reg("tx",       "tx psf|mif [count] - inject action frame(s)", cmd_tx);
	reg("txdata",   "txdata <hexpayload> - inject one AWDL data frame", cmd_txdata);
	reg("peers",    "print the current peer table (name, age, channel seq)", cmd_peers);
	reg("sync",     "print master, sync-tree height, sync error, peer count", cmd_sync);
	reg("stats",    "print rx/tx counters", cmd_stats);
	reg("reset",    "zero the counters", cmd_reset);
	reg("rxprobe",  "length fields of the last AWDL action frame (FCS check)", cmd_rxprobe);
	reg("chanlat",  "chanlat [n] [c1,c2,...] - measure channel-switch latency", cmd_chanlat);
	reg("txpwr",    "txpwr [8..84] - show/set TX power ceiling (0.25 dBm)", cmd_txpwr);
	reg("usbbench", "usbbench [kib] - stream data for host-side USB timing", cmd_usbbench);
}
