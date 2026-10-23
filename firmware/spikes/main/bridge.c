/*
 * EspDrop - USB host-link bridge transport (ESP32). See bridge.h.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "bridge.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_system.h"   /* esp_get_free_heap_size */
#include "esp_timer.h"
#include "hal/usb_serial_jtag_ll.h"   /* `usbtest raw`: write the FIFO directly */
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"

#include "awdl_cobs.h"
#include "awdl_app.h" /* awdl_app_resync_peers, for the `resync` command */
#include "ble_wake.h" /* `wake`: the AirDrop wake over our own Bluetooth */
#include "radio.h"    /* radio_get_stats, for the `radio` diagnostic command */

#define BRIDGE_MAX_PAYLOAD 1600
#define BRIDGE_MAX_MSG     (1 + BRIDGE_MAX_PAYLOAD)   /* type + payload */
#define USB_RX_BUF         1024
/* Large enough for several full frames, so the driver can keep feeding the
 * 64-byte USB FIFO while the forward task COBS-encodes the next frame. */
#define USB_TX_BUF         8192

/* Device->host RX forwarding: one Ethernet frame per queue slot. The Wi-Fi task
 * only builds+enqueues (never blocks on USB); a dedicated task drains and sends. */
#define BRIDGE_RX_PAYLOAD_MAX 1500                 /* L3 payload cap (awdl0 MTU) */
/* One AWDL window delivers ~15 frames in ~50 ms (airdrop-mt7921 FINDINGS), much
 * faster than USB drains them, so the queue must absorb a whole burst; on 5 GHz
 * an iPhone sends in 15 of 16 slots, so bursts are longer still (C5: 299 drops
 * at depth 32 in one transfer). Try the big depth first and fall back if the
 * heap is short. */
static const uint8_t k_rx_q_depths[] = {64, 48, 32, 24, 16, 8};
/* Wi-Fi allocates its RX/TX buffers dynamically from the same heap, so never
 * take the last of it: a larger depth is used only if this much stays free. */
#define BRIDGE_HEAP_RESERVE (64u * 1024u)
typedef struct {
	uint16_t len;
	uint8_t  eth[14 + BRIDGE_RX_PAYLOAD_MAX];
} rx_frame_t;

/* Peer add/remove notifications, queued by the AWDL task and sent by their own
 * task, so the AWDL timing task never blocks on a USB write. */
#define BRIDGE_PEER_Q_LEN 16
typedef struct {
	bool    added;
	uint8_t mac[6];
} peer_evt_t;

static SemaphoreHandle_t s_tx_lock;
static QueueHandle_t     s_rx_fwd_q;   /* rx_frame_t: Wi-Fi task -> fwd task */
static QueueHandle_t     s_peer_q;     /* peer_evt_t: AWDL task -> peer task */
static uint8_t           s_rx_q_depth; /* depth actually allocated */
static bool s_started;

/* Counters touched only from the single RX task (handle_msg), so no locking. */
static uint32_t s_frames_from_host;
static uint32_t s_bytes_from_host;
/* Touched across tasks (written by Wi-Fi/fwd tasks, read by the stats command).
 * 32-bit aligned loads/stores are atomic on the C3, and a stale stats read is
 * harmless, so these are volatile rather than locked. */
static volatile uint32_t s_frames_to_host;
static volatile uint32_t s_bytes_to_host;
static volatile uint32_t s_rx_drops;

/* Sink for host->device Ethernet frames (set by app_main to the AWDL TX path). */
static bridge_frame_sink_t s_frame_sink;
static void               *s_frame_sink_ctx;

void bridge_set_frame_sink(bridge_frame_sink_t fn, void *ctx) {
	s_frame_sink_ctx = ctx;
	s_frame_sink = fn;
}

/* Emit one synthetic Ethernet frame to the host so the device->host->awdl0 path
 * can be exercised before any radio exists (M2.1). ff:ff:ff:ff:ff:ff <- our MAC,
 * ethertype 0x88b5 (IEEE local experimental, inert) + a marker body. */
static void send_test_frame(void) {
	uint8_t mac[6] = {0};
	esp_read_mac(mac, ESP_MAC_WIFI_STA);
	static const char body[] = "EspDrop M2.1 testframe";
	uint8_t eth[14 + sizeof(body) - 1];
	memset(eth, 0xff, 6);          /* destination: broadcast */
	memcpy(eth + 6, mac, 6);       /* source: our Wi-Fi MAC */
	eth[12] = 0x88;
	eth[13] = 0xb5;                /* ethertype 0x88b5 */
	memcpy(eth + 14, body, sizeof(body) - 1);
	bridge_send(BRIDGE_MSG_FRAME, eth, sizeof(eth));
}

/* Wi-Fi task context: build the Ethernet frame into a scratch slot and enqueue
 * it for the forwarding task. rx_cb is serialized (one Wi-Fi task), so a single
 * static scratch buffer is safe and keeps this off the Wi-Fi task's stack. */
void bridge_rx_frame(const uint8_t dst[6], const uint8_t src[6], uint16_t ethertype,
                     const uint8_t *payload, size_t plen, void *ctx) {
	(void)ctx;
	static rx_frame_t scratch;
	if (!s_rx_fwd_q) return;
	if (plen > BRIDGE_RX_PAYLOAD_MAX) { s_rx_drops++; return; }
	memcpy(scratch.eth, dst, 6);
	memcpy(scratch.eth + 6, src, 6);
	scratch.eth[12] = (uint8_t)(ethertype >> 8);
	scratch.eth[13] = (uint8_t)(ethertype & 0xff);
	if (plen) memcpy(scratch.eth + 14, payload, plen);
	scratch.len = (uint16_t)(14 + plen);
	if (xQueueSend(s_rx_fwd_q, &scratch, 0) != pdTRUE)
		s_rx_drops++; /* host not keeping up: drop rather than stall Wi-Fi */
}

/* Drain the RX-forward queue to the host. A dedicated task so the (bounded but
 * blocking) USB write never runs on the Wi-Fi task or the AWDL timing task. */
static void bridge_fwd_task(void *arg) {
	(void)arg;
	static rx_frame_t fr;
	for (;;) {
		if (xQueueReceive(s_rx_fwd_q, &fr, portMAX_DELAY) == pdTRUE) {
			if (bridge_send(BRIDGE_MSG_FRAME, fr.eth, fr.len) == 0) {
				s_frames_to_host++;
				s_bytes_to_host += fr.len;
			}
		}
	}
}

void bridge_notify_peer(bool added, const uint8_t mac[6]) {
	if (!s_peer_q) return;
	peer_evt_t ev;
	ev.added = added;
	memcpy(ev.mac, mac, 6);
	/* Never block the caller (the AWDL task). On overflow the event is dropped;
	 * the host recovers the full peer set with `resync`. */
	(void)xQueueSend(s_peer_q, &ev, 0);
}

/* Drain peer notifications to the host (USB write off the AWDL timing task). */
static void bridge_peer_task(void *arg) {
	(void)arg;
	peer_evt_t ev;
	for (;;) {
		if (xQueueReceive(s_peer_q, &ev, portMAX_DELAY) == pdTRUE)
			bridge_send(ev.added ? BRIDGE_MSG_PEER_ADD : BRIDGE_MSG_PEER_REMOVE,
			            ev.mac, 6);
	}
}

/* Send a NUL-terminated string as a CMD_REPLY (length from strlen, so a
 * truncated snprintf can never be reported as longer than the buffer). */
static void cmd_reply(const char *s) {
	bridge_send(BRIDGE_MSG_CMD_REPLY, s, strlen(s));
}

/* A short text command from the host (BRIDGE_MSG_CMD) -> a CMD_REPLY. */
/*
 * `usbtest raw [kib]`: the same dummy frames as `usbtest`, but written straight
 * into the 64-byte USB FIFO by polling, instead of via the driver, which
 * refills the FIFO from its interrupt only after the host has fetched the last
 * packet. If this is much faster, the refill latency is the limit; if not, it
 * is the host's polling. Holds the TX lock and masks the driver's TX interrupt
 * meanwhile; a test tool, run it while nothing else is going on.
 */
static void usbtest_raw(int kib, char *out, size_t cap) {
	if (kib < 1) kib = 1;
	if (kib > 8192) kib = 8192;
	static uint8_t msg[1 + 14 + 1400];
	static uint8_t enc[COBS_ENCODE_BOUND(sizeof(msg)) + 1];
	msg[0] = BRIDGE_MSG_FRAME;
	memset(msg + 1, 0xff, 6);
	radio_get_self_mac(msg + 7);
	msg[13] = 0x88; msg[14] = 0xb5;
	memset(msg + 15, 0x5a, sizeof(msg) - 15);
	size_t enc_len = cobs_encode(msg, sizeof(msg), enc, sizeof(enc) - 1);
	enc[enc_len++] = 0x00;

	xSemaphoreTake(s_tx_lock, portMAX_DELAY);
	vTaskDelay(pdMS_TO_TICKS(100));   /* let the driver drain its ring buffer */
	usb_serial_jtag_ll_disable_intr_mask(USB_SERIAL_JTAG_INTR_SERIAL_IN_EMPTY);
	size_t total = (size_t)kib * 1024, sent = 0;
	bool stalled = false;
	int64_t t0 = esp_timer_get_time(), next_yield = t0 + 50000;
	while (sent < total && !stalled) {
		const uint8_t *q = enc;
		size_t left = enc_len;
		while (left) {
			int64_t give_up = esp_timer_get_time() + 1000000;
			while (!usb_serial_jtag_ll_txfifo_writable())
				if (esp_timer_get_time() > give_up) { stalled = true; break; }
			if (stalled) break;
			int w = usb_serial_jtag_ll_write_txfifo(q, left > 64 ? 64 : (uint32_t)left);
			usb_serial_jtag_ll_txfifo_flush();
			q += w;
			left -= (size_t)w;
		}
		if (!stalled) sent += sizeof(msg) - 1;
		if (esp_timer_get_time() > next_yield) {   /* keep the idle task (WDT) alive */
			vTaskDelay(1);
			next_yield = esp_timer_get_time() + 50000;
		}
	}
	int64_t dt = esp_timer_get_time() - t0;
	usb_serial_jtag_ll_ena_intr_mask(USB_SERIAL_JTAG_INTR_SERIAL_IN_EMPTY);
	xSemaphoreGive(s_tx_lock);
	snprintf(out, cap, "usbtest raw: %u bytes in %u ms = %u kB/s%s",
	         (unsigned)sent, (unsigned)(dt / 1000),
	         dt > 0 ? (unsigned)((uint64_t)sent * 1000u / (uint64_t)dt) : 0u,
	         stalled ? " (host stopped reading)" : "");
}

static void handle_cmd(const uint8_t *p, size_t n) {
	char cmd[64];
	size_t k = n < sizeof(cmd) - 1 ? n : sizeof(cmd) - 1;
	memcpy(cmd, p, k);
	cmd[k] = '\0';
	while (k && (cmd[k - 1] == '\n' || cmd[k - 1] == '\r' || cmd[k - 1] == ' '))
		cmd[--k] = '\0';

	char out[192];
	if (strcmp(cmd, "ping") == 0) {
		cmd_reply("pong");
	} else if (strcmp(cmd, "mac") == 0) {
		uint8_t mac[6] = {0};
		esp_read_mac(mac, ESP_MAC_WIFI_STA);
		snprintf(out, sizeof(out), "mac %02x:%02x:%02x:%02x:%02x:%02x",
		         mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
		cmd_reply(out);
	} else if (strcmp(cmd, "stats") == 0) {
		snprintf(out, sizeof(out),
		         "frames_from_host=%u bytes_from_host=%u frames_to_host=%u bytes_to_host=%u "
		         "rx_drops=%u rxq=%u heap=%u heap_min=%u",
		         (unsigned)s_frames_from_host, (unsigned)s_bytes_from_host,
		         (unsigned)s_frames_to_host, (unsigned)s_bytes_to_host, (unsigned)s_rx_drops,
		         (unsigned)s_rx_q_depth, (unsigned)esp_get_free_heap_size(),
		         (unsigned)esp_get_minimum_free_heap_size());
		cmd_reply(out);
	} else if (strcmp(cmd, "radio") == 0) {
		radio_stats_t st;
		radio_get_stats(&st);
		char rbuf[256];
		/* tx_ack = unicast frames the peer hardware-ACKed (our TX reached it);
		 * for broadcast/multicast tx_ack is always success. Counts are cast to
		 * 32-bit (ample for a test run) and printed with %u, matching `stats`. */
		snprintf(rbuf, sizeof(rbuf),
		         "rx_action=%u rx_data=%u rx_dup=%u rx_amsdu=%u rx_msdu=%u rx_dperr=%u "
		         "rx_other=%u rx_perr=%u tx_ok=%u tx_err=%u tx_ack=%u tx_noack=%u",
		         (unsigned)st.rx_action, (unsigned)st.rx_data, (unsigned)st.rx_dup,
		         (unsigned)st.rx_amsdu, (unsigned)st.rx_msdu, (unsigned)st.rx_data_perr,
		         (unsigned)st.rx_other_bssid, (unsigned)st.rx_parse_err,
		         (unsigned)st.tx_ok, (unsigned)st.tx_err,
		         (unsigned)st.tx_done_ok, (unsigned)st.tx_done_fail);
		cmd_reply(rbuf);
	} else if (strcmp(cmd, "node") == 0) {
		/* AWDL node state: who we sync to and which slots we advertise. If iOS
		 * stops sending to us, check here that the master is the iPhone and that
		 * our sequence is sparse (mirrored), not 16x the same channel. */
		awdl_node_status_t ns;
		char nbuf[320];
		if (!awdl_app_status(&ns)) {
			cmd_reply("node: awdl not started");
		} else {
			int o = snprintf(nbuf, sizeof(nbuf),
			                 "master=%02x:%02x:%02x:%02x:%02x:%02x%s peers=%d height=%u "
			                 "sync_err=%d chan=%u auto=%s mode=%s derived=%s seq=",
			                 ns.master_addr[0], ns.master_addr[1], ns.master_addr[2],
			                 ns.master_addr[3], ns.master_addr[4], ns.master_addr[5],
			                 ns.have_master ? "" : "(self)", ns.peer_count,
			                 (unsigned)ns.height, (int)ns.last_sync_err_tu,
			                 (unsigned)radio_get_channel(),
			                 awdl_app_auto_channel() ? "on" : "off",
			                 ns.seq_mode == AWDL_SEQ_VERBATIM ? "verbatim" : "mirror",
			                 ns.seq_derived ? "yes" : "no");
			for (unsigned i = 0; i < AWDL_CHANSEQ_LEN && o > 0 && o < (int)sizeof(nbuf); i++)
				o += snprintf(nbuf + o, sizeof(nbuf) - (size_t)o, "%s%u",
				              i ? "," : "", ns.self_chanseq[i]);
			if (o > 0 && o < (int)sizeof(nbuf))
				o += snprintf(nbuf + o, sizeof(nbuf) - (size_t)o, " mseq=");
			for (unsigned i = 0; i < AWDL_CHANSEQ_LEN && o > 0 && o < (int)sizeof(nbuf); i++)
				o += ns.master_has_chanseq
				     ? snprintf(nbuf + o, sizeof(nbuf) - (size_t)o, "%s%u",
				                i ? "," : "", ns.master_chanseq[i])
				     : (i ? 0 : snprintf(nbuf + o, sizeof(nbuf) - (size_t)o, "-"));
			cmd_reply(nbuf);
		}
	} else if (strcmp(cmd, "tx") == 0) {
		/* Host->radio data path: did the frame reach the radio, and did the peer
		 * ACK our unicast data frames (broadcast PSF/MIF excluded)? */
		awdl_app_tx_counters_t c;
		awdl_app_get_tx_counters(&c);
		radio_stats_t st;
		radio_get_stats(&st);
		snprintf(out, sizeof(out),
		         "queued=%u q_drop=%u sent_uc=%u sent_mc=%u fail=%u uc_ack=%u uc_noack=%u",
		         (unsigned)c.queued, (unsigned)c.q_drop, (unsigned)c.sent_uc,
		         (unsigned)c.sent_mc, (unsigned)c.tx_fail,
		         (unsigned)st.tx_uc_ack, (unsigned)st.tx_uc_noack);
		cmd_reply(out);
	} else if (strcmp(cmd, "chan") == 0 || strncmp(cmd, "chan ", 5) == 0) {
		/* `chan` shows the channel and whether auto is on; `chan <n>` pins the
		 * node to a channel (keeps sync/peers); `chan auto` follows the master
		 * onto its preferred 5 GHz channel (44/149) on its own. */
		const char *arg = cmd[4] == '\0' ? "" : cmd + 5;
		if (arg[0] == '\0') {
			snprintf(out, sizeof(out), "chan %u (auto %s)", radio_get_channel(),
			         awdl_app_auto_channel() ? "on" : "off");
		} else if (strcmp(arg, "auto") == 0) {
			awdl_app_set_auto_channel(true);
			snprintf(out, sizeof(out), "chan auto on (now %u)", radio_get_channel());
		} else {
			int ch = atoi(arg);
			int err = (ch >= 1 && ch <= 255 && radio_channel_supported((uint8_t)ch))
			          ? awdl_app_set_channel((uint8_t)ch, false) : ESP_ERR_NOT_SUPPORTED;
			if (err != ESP_OK)
				snprintf(out, sizeof(out), "chan %d: %s", ch, esp_err_to_name(err));
			else
				snprintf(out, sizeof(out), "chan %d (auto off)", ch);
		}
		cmd_reply(out);
	} else if (strcmp(cmd, "seq") == 0 || strncmp(cmd, "seq ", 4) == 0) {
		/* `seq` shows how our advertised sequence follows the master's; `seq
		 * mirror` (default) puts our channel only where the master is on it,
		 * `seq verbatim` copies the master's sequence unchanged. An experiment:
		 * which one the iPhone sends unicast to (see `node`, ping6). */
		const char *arg = cmd[3] == '\0' ? "" : cmd + 4;
		if (strcmp(arg, "mirror") == 0)
			awdl_app_set_seq_mode(AWDL_SEQ_MIRROR);
		else if (strcmp(arg, "verbatim") == 0)
			awdl_app_set_seq_mode(AWDL_SEQ_VERBATIM);
		if (arg[0] && strcmp(arg, "mirror") && strcmp(arg, "verbatim"))
			snprintf(out, sizeof(out), "seq: use `seq`, `seq mirror` or `seq verbatim`");
		else
			snprintf(out, sizeof(out), "seq %s",
			         awdl_app_seq_mode() == AWDL_SEQ_VERBATIM ? "verbatim" : "mirror");
		cmd_reply(out);
	} else if (strcmp(cmd, "wake") == 0 || strncmp(cmd, "wake ", 5) == 0) {
		/* AirDrop wake over our own Bluetooth (ble_wake.h): `wake` shows it,
		 * `wake <secs> [16 hex digits]` advertises for secs seconds (restarts a
		 * running one) with the four 2-byte contact hashes, `wake off` stops. */
		const char *arg = cmd[4] == '\0' ? "" : cmd + 5;
		if (!ble_wake_ready()) {
			snprintf(out, sizeof(out), "wake unavailable (no Bluetooth in this "
			         "firmware, or it is still starting)");
		} else if (arg[0] == '\0') {
			unsigned left = ble_wake_remaining();
			if (left) snprintf(out, sizeof(out), "wake on %u s left", left);
			else snprintf(out, sizeof(out), "wake off");
		} else if (strcmp(arg, "off") == 0) {
			int rc = ble_wake_stop();
			if (rc) snprintf(out, sizeof(out), "wake error %d", rc);
			else snprintf(out, sizeof(out), "wake off");
		} else {
			char *end;
			long secs = strtol(arg, &end, 10);
			while (*end == ' ') end++;
			uint8_t h[8];
			bool have_h = false, bad = end == arg || secs < 1 || secs > BLE_WAKE_MAX_SECS;
			if (!bad && *end) {
				bad = strlen(end) != 16;
				for (int i = 0; !bad && i < 8; i++) {
					char hx[3] = {end[2 * i], end[2 * i + 1], 0};
					char *e2;
					h[i] = (uint8_t)strtoul(hx, &e2, 16);
					bad = *e2 != '\0' || !hx[0] || !hx[1];
				}
				have_h = !bad;
			}
			if (bad) {
				snprintf(out, sizeof(out), "wake: use `wake`, `wake off` or "
				         "`wake <1..%d> [16 hex digits]`", BLE_WAKE_MAX_SECS);
			} else {
				int rc = ble_wake_start((unsigned)secs, have_h ? h : NULL);
				if (rc) snprintf(out, sizeof(out), "wake error %d", rc);
				else snprintf(out, sizeof(out), "wake on %u s left", ble_wake_remaining());
			}
		}
		cmd_reply(out);
	} else if (strcmp(cmd, "checks") == 0) {
		/* Bring-up checks on a new chip: is the frame length right (FCS where
		 * we expect it, IPv6 packets neither cut short nor padded)? */
		radio_stats_t st;
		radio_get_stats(&st);
		snprintf(out, sizeof(out),
		         "fcs_ok=%u fcs_bad=%u fcs_at_sig=%u fcs_at_dump=%u type_mm=%u "
		         "ip6_short=%u ip6_long=%u",
		         (unsigned)st.rx_fcs_ok, (unsigned)st.rx_fcs_bad,
		         (unsigned)st.rx_fcs_at_sig, (unsigned)st.rx_fcs_at_dump,
		         (unsigned)st.rx_type_mismatch, (unsigned)st.rx_ip6_short,
		         (unsigned)st.rx_ip6_long);
		cmd_reply(out);
	} else if (strcmp(cmd, "usbtest raw") == 0 || strncmp(cmd, "usbtest raw ", 12) == 0) {
		int kib = cmd[11] ? atoi(cmd + 12) : 1024;
		usbtest_raw(kib, out, sizeof(out));
		cmd_reply(out);
	} else if (strcmp(cmd, "usbtest") == 0 || strncmp(cmd, "usbtest ", 8) == 0) {
		/* Device->host ceiling of the bridge path: stream full-size dummy
		 * Ethernet frames (ethertype 0x88b5, ignored by the host stack) through
		 * the same COBS/USB path as received AWDL data, and time it. Run it
		 * while no transfer is going on: this task is busy meanwhile. */
		int kib = cmd[7] ? atoi(cmd + 8) : 1024;
		if (kib < 1) kib = 1;
		if (kib > 8192) kib = 8192;
		static uint8_t f[14 + 1400];
		memset(f, 0xff, 6);
		radio_get_self_mac(f + 6);
		f[12] = 0x88; f[13] = 0xb5;
		memset(f + 14, 0x5a, sizeof(f) - 14);
		size_t total = (size_t)kib * 1024, sent = 0;
		int fails = 0;
		int64_t t0 = esp_timer_get_time();
		while (sent < total && fails < 10) {
			if (bridge_send(BRIDGE_MSG_FRAME, f, sizeof(f)) == 0) sent += sizeof(f);
			else fails++;
		}
		int64_t dt = esp_timer_get_time() - t0;
		snprintf(out, sizeof(out), "usbtest: %u bytes in %u ms = %u kB/s%s",
		         (unsigned)sent, (unsigned)(dt / 1000),
		         dt > 0 ? (unsigned)((uint64_t)sent * 1000u / (uint64_t)dt) : 0u,
		         fails ? " (writes timed out)" : "");
		cmd_reply(out);
	} else if (strcmp(cmd, "testframe") == 0) {
		send_test_frame();
		cmd_reply("sent testframe");
	} else if (strcmp(cmd, "resync") == 0) {
		/* Re-announce every known peer on the next AWDL tick. The host sends this
		 * on connect, because the C3 keeps running across host sessions (its USB
		 * port does not reset the chip) and would otherwise never re-emit peers
		 * it already knew before the host attached. */
		awdl_app_resync_peers();
		cmd_reply("resync");
	} else {
		snprintf(out, sizeof(out), "unknown cmd '%s'", cmd);
		cmd_reply(out);
	}
}

/* Dispatch a fully decoded message (type byte + payload). */
static void handle_msg(const uint8_t *msg, size_t len) {
	if (len < 1) return;
	uint8_t type = msg[0];
	const uint8_t *payload = msg + 1;
	size_t plen = len - 1;

	switch (type) {
	case BRIDGE_MSG_ECHO:
		bridge_send(BRIDGE_MSG_ECHO_REPLY, payload, plen);
		break;
	case BRIDGE_MSG_FRAME:
		/* Host -> device Ethernet frame for awdl0. Account it, then hand it to the
		 * TX sink (the AWDL data-TX path) if one is registered. */
		s_frames_from_host++;
		s_bytes_from_host += (uint32_t)plen;
		if (s_frame_sink)
			s_frame_sink(payload, plen, s_frame_sink_ctx);
		break;
	case BRIDGE_MSG_CMD:
		handle_cmd(payload, plen);
		break;
	/* PEER_* are device->host only (see bridge_notify_peer). */
	default:
		break;
	}
}

static void bridge_rx_task(void *arg) {
	(void)arg;
	static cobs_rx_t rx;
	static uint8_t out[BRIDGE_MAX_MSG];
	uint8_t chunk[256];
	cobs_rx_init(&rx);

	for (;;) {
		int n = usb_serial_jtag_read_bytes(chunk, sizeof(chunk), portMAX_DELAY);
		if (n <= 0) continue;
		for (int i = 0; i < n; i++) {
			int r = cobs_rx_byte(&rx, chunk[i], out, sizeof(out));
			if (r > 0)
				handle_msg(out, (size_t)r);
			/* r == -1: malformed/overflowed frame, already reset; ignore. */
		}
	}
}

int bridge_start(void) {
	if (s_started) return 0;

	s_tx_lock = xSemaphoreCreateMutex();
	if (!s_tx_lock) return -1;

	usb_serial_jtag_driver_config_t cfg = {
		.tx_buffer_size = USB_TX_BUF,
		.rx_buffer_size = USB_RX_BUF,
	};
	esp_err_t e = usb_serial_jtag_driver_install(&cfg);
	if (e != ESP_OK && e != ESP_ERR_INVALID_STATE) { /* INVALID_STATE: console already installed it */
		vSemaphoreDelete(s_tx_lock);
		s_tx_lock = NULL;
		return e;
	}

	for (size_t i = 0; i < sizeof(k_rx_q_depths) && !s_rx_fwd_q; i++) {
		size_t need = (size_t)k_rx_q_depths[i] * sizeof(rx_frame_t);
		bool last = (i + 1 == sizeof(k_rx_q_depths));
		if (!last && esp_get_free_heap_size() < need + BRIDGE_HEAP_RESERVE)
			continue;   /* keep headroom for Wi-Fi; the smallest depth is always tried */
		s_rx_fwd_q = xQueueCreate(k_rx_q_depths[i], sizeof(rx_frame_t));
		if (s_rx_fwd_q) s_rx_q_depth = k_rx_q_depths[i];
	}
	s_peer_q   = xQueueCreate(BRIDGE_PEER_Q_LEN, sizeof(peer_evt_t));
	if (!s_rx_fwd_q || !s_peer_q) {
		if (s_rx_fwd_q) vQueueDelete(s_rx_fwd_q);
		if (s_peer_q)   vQueueDelete(s_peer_q);
		s_rx_fwd_q = NULL;
		s_peer_q = NULL;
		vSemaphoreDelete(s_tx_lock);
		s_tx_lock = NULL;
		return -1;
	}

	/* Reduce corruption of the binary frame stream by silencing ESP_LOG. This is
	 * not absolute: ROM/early-boot output (before this runs) and the panic
	 * handler (esp_rom_printf, crash only) still reach the same peripheral. Those
	 * are pre-stream or crash-only, and COBS resynchronises at the next 0x00
	 * delimiter, so steady-state framing is unaffected. */
	esp_log_level_set("*", ESP_LOG_NONE);

	/* Set started before creating the task: the RX task runs at a higher
	 * priority and may dispatch (and reply to) a queued ECHO before xTaskCreate
	 * returns; bridge_send must not reject that first reply. */
	s_started = true;
	if (xTaskCreate(bridge_rx_task, "bridge_rx", 4096, NULL, 6, NULL) != pdPASS) {
		s_started = false;
		vQueueDelete(s_rx_fwd_q);
		s_rx_fwd_q = NULL;
		vQueueDelete(s_peer_q);
		s_peer_q = NULL;
		vSemaphoreDelete(s_tx_lock);
		s_tx_lock = NULL;
		/* Logging was just silenced; restore it so the caller's error is visible
		 * (nothing was transmitted yet, so no COBS stream to corrupt). */
		esp_log_level_set("*", ESP_LOG_ERROR);
		return -1;
	}
	/* The forwarding task is best-effort: if it fails to start, RX frames are
	 * dropped (queue fills) but the control link stays up, so do not fail. */
	if (xTaskCreate(bridge_fwd_task, "bridge_fwd", 3072, NULL, 5, NULL) != pdPASS)
		bridge_log("warning: rx-forward task did not start");
	if (xTaskCreate(bridge_peer_task, "bridge_peer", 2560, NULL, 4, NULL) != pdPASS)
		bridge_log("warning: peer-notify task did not start");

	bridge_log("bridge up");
	return 0;
}

int bridge_send(uint8_t type, const void *payload, size_t len) {
	if (!s_started || len > BRIDGE_MAX_PAYLOAD) return -1;

	static uint8_t msg[BRIDGE_MAX_MSG];
	static uint8_t enc[COBS_ENCODE_BOUND(BRIDGE_MAX_MSG) + 1];

	xSemaphoreTake(s_tx_lock, portMAX_DELAY);
	msg[0] = type;
	if (len && payload) memcpy(msg + 1, payload, len);
	size_t mlen = 1 + len;
	size_t enc_len = cobs_encode(msg, mlen, enc, sizeof(enc) - 1);
	int ret = -1;
	if (enc_len > 0) {
		enc[enc_len++] = 0x00; /* frame delimiter */
		/* Write the whole frame, looping over short writes. A bounded timeout
		 * keeps a host that stops draining from wedging the lock (and thus every
		 * other sender) forever; on timeout we drop the rest of this frame. */
		size_t off = 0;
		ret = 0;
		while (off < enc_len) {
			int w = usb_serial_jtag_write_bytes(enc + off, enc_len - off,
			                                    pdMS_TO_TICKS(1000));
			if (w <= 0) { ret = -1; break; } /* host not draining: drop */
			off += (size_t)w;
		}
	}
	xSemaphoreGive(s_tx_lock);
	return ret;
}

void bridge_log(const char *fmt, ...) {
	char buf[200];
	va_list ap;
	va_start(ap, fmt);
	int n = vsnprintf(buf, sizeof(buf), fmt, ap);
	va_end(ap);
	if (n < 0) return;
	if (n > (int)sizeof(buf) - 1) n = sizeof(buf) - 1;
	bridge_send(BRIDGE_MSG_LOG, buf, (size_t)n);
}
