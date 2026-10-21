/*
 * EspDrop - AWDL frame construction and parsing (platform independent).
 *
 * Builds and parses the frames of Apple Wireless Direct Link (AWDL), the
 * ad-hoc Wi-Fi link that AirDrop runs on. The protocol was documented by the
 * Open Wireless Link project (TU Darmstadt) through reverse engineering and
 * implemented in OWL (https://github.com/seemoo-lab/owl). The frame layouts,
 * TLV type numbers and field offsets here follow OWL's src/frame.h, src/tx.c
 * and src/rx.c:
 *   Copyright (C) 2018  The Open Wireless Link Project
 *   Copyright (C) 2018  Milan Stute
 * Reimplemented for EspDrop against an offset-based wire API so it is endian-
 * and alignment-safe and testable on a host:
 *   Copyright (C) 2026  EspDrop contributors
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * This module depends only on the C standard library (via awdl_wire.h). It can
 * be compiled and unit tested natively; the ESP-IDF radio glue lives elsewhere.
 *
 * Buffers passed in and out start at the IEEE 802.11 MAC header. There is never
 * a radiotap header (the ESP32 promiscuous callback delivers the MAC frame and
 * reports PHY metadata separately) and never a trailing FCS (hardware appends
 * it on transmit; the caller strips it before parsing).
 */
#ifndef ESPDROP_AWDL_FRAME_H
#define ESPDROP_AWDL_FRAME_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------------ */
/* Constants                                                                */
/* ------------------------------------------------------------------------ */

#define AWDL_TU_US            1024u  /* one IEEE 802.11 time unit, microseconds */
#define AWDL_AW_PERIOD_TU       16u  /* availability window length */
#define AWDL_PRESENCE_MODE       4u  /* AWs per extended AW (EAW) */
#define AWDL_EAW_TU             (AWDL_AW_PERIOD_TU * AWDL_PRESENCE_MODE) /* 64 TU, one slot */
#define AWDL_CHANSEQ_LEN        16u  /* slots in a channel sequence */
#define AWDL_PSF_INTERVAL_TU   110u  /* OWL master PSF interval */

#define AWDL_NAME_MAX           63u  /* max host name length we store (excl. NUL) */

#define AWDL_SUBTYPE_PSF         0u  /* periodic synchronization frame */
#define AWDL_SUBTYPE_MIF         3u  /* master indication frame */

#define AWDL_VERSION_OWL      0x34u  /* AWDL 3.4, as announced by OWL's version TLV */
#define AWDL_ACTION_VERSION   0x10u  /* version byte inside the action header (1.0) */
#define AWDL_ACTION_TYPE      0x08u  /* AWDL action type */
#define AWDL_CATEGORY_VENDOR   127u  /* IEEE 802.11 vendor-specific action category */

#define AWDL_DEVCLASS_MACOS      1u
#define AWDL_DEVCLASS_IOS        2u
#define AWDL_DEVCLASS_TVOS       8u

#define AWDL_ELECTION_METRIC_INIT  60u  /* OWL default: stays a slave */
#define AWDL_ELECTION_COUNTER_INIT  0u

#define AWDL_LLC_PID        0x0800u  /* protocol id in the LLC/SNAP header */
#define AWDL_DATA_HEAD      0x0403u  /* first field of the AWDL data header */
#define ETHERTYPE_IPV6      0x86ddu

/* IEEE 802.11 frame control, as the little-endian value of the first 2 bytes */
#define FC_ACTION           0x00d0u  /* type management, subtype action (13) */
#define FC_DATA             0x0008u  /* type data, subtype data (0) */
#define IEEE80211_HDR_LEN       24u

/* AWDL TLV types (subset; full list in OWL src/frame.h) */
enum awdl_tlv_type {
	AWDL_TLV_SYNC_PARAMS       = 4,
	AWDL_TLV_ELECTION_PARAMS   = 5,
	AWDL_TLV_SERVICE_PARAMS    = 6,
	AWDL_TLV_HT_CAPABILITIES   = 7,
	AWDL_TLV_DATA_PATH_STATE   = 12,
	AWDL_TLV_ARPA              = 16,
	AWDL_TLV_CHAN_SEQ          = 18,
	AWDL_TLV_VERSION           = 21,
	AWDL_TLV_ELECTION_PARAMS_V2 = 24,
};

/* Channel sequence encodings */
enum awdl_chan_enc {
	AWDL_CHAN_ENC_SIMPLE  = 0, /* 1 byte: channel number */
	AWDL_CHAN_ENC_LEGACY  = 1, /* 2 bytes: flags, channel number */
	AWDL_CHAN_ENC_OPCLASS = 3, /* 2 bytes: channel number, operating class */
};

/* Data path state TLV flags (what OWL sets) */
#define AWDL_DPS_FLAGS_OWL  0x8f24u

/* Return codes (negative = error) */
enum awdl_result {
	AWDL_OK          =  0,
	AWDL_ERR_SHORT   = -1, /* buffer/TLV truncated */
	AWDL_ERR_NOT_AWDL = -2, /* wrong frame control / category / OUI / type */
	AWDL_ERR_ARG     = -3, /* bad argument or output buffer too small */
};

extern const uint8_t AWDL_BSSID[6];     /* 00:25:00:ff:94:73 */
extern const uint8_t AWDL_OUI[3];       /* 00:17:f2 */
extern const uint8_t AWDL_BROADCAST[6]; /* ff:ff:ff:ff:ff:ff */

/* ------------------------------------------------------------------------ */
/* Transmitter state (what the frame builder needs to know about us)        */
/* ------------------------------------------------------------------------ */

/*
 * The spike firmware does not yet synchronise to a master. Its AW clock runs
 * freely from a monotonic microsecond source, and it advertises itself as its
 * own master with OWL's low election metric, so a real Apple device will win
 * any election. That is enough to be discovered and to emit well-formed PSF/MIF
 * frames; proper synchronisation is a later milestone.
 */
typedef struct {
	uint8_t  self_addr[6];    /* our AWDL MAC: addr2, election self, DPS address */
	char     name[AWDL_NAME_MAX + 1]; /* host name advertised in the ARPA TLV */
	uint8_t  version;         /* version TLV value, e.g. AWDL_VERSION_OWL */
	uint8_t  devclass;        /* version TLV device class */
	uint8_t  chan_enc;        /* enum awdl_chan_enc for our own sequence */
	uint8_t  seq[AWDL_CHANSEQ_LEN][2]; /* our channel sequence, encoded bytes */
	uint8_t  master_chan;     /* channel number we call our master channel */
	uint16_t seq_num;         /* AWDL data sequence number (incremented on data) */
	uint16_t ieee_seq_num;    /* IEEE 802.11 sequence number (12 bits) */
} awdl_self_t;

/*
 * Initialise self state with OWL-compatible defaults and a static single-channel
 * sequence (all 16 slots on chan_num, opclass encoding). chan_num is the social
 * channel to park on, e.g. 6 on the 2.4 GHz-only ESP32-C3.
 */
void awdl_self_init(awdl_self_t *self, const uint8_t self_addr[6],
                    const char *name, uint8_t chan_num);

/* Fill our channel sequence with a single channel in all 16 slots (the lone,
 * not-yet-synced default). */
void awdl_self_set_chanseq_static(awdl_self_t *self, uint8_t chan_num);

/*
 * Mirror a master's channel sequence into ours: advertise keep_chan only in the
 * slots where the master is on keep_chan, and "absent" (0) everywhere else. On a
 * single-band device this is the honest sequence (we can only be on keep_chan),
 * and far closer to the sparse shape Apple emits than a flat all-one-channel
 * sequence, which iOS rejects (airdrop-mt7921 findings; docs/UEBERBLICK.md).
 *
 * Returns false and leaves our sequence unchanged if the master names keep_chan
 * in NO slot (we would otherwise advertise presence nowhere). The caller decides
 * what to advertise then; awdl_node keeps the last good mirror.
 */
bool awdl_self_set_chanseq_intersect(awdl_self_t *self,
                                     const uint8_t master_chanseq[AWDL_CHANSEQ_LEN],
                                     uint8_t keep_chan);

/* ------------------------------------------------------------------------ */
/* Parsed peer view (what the parser extracts from a received action frame)  */
/* ------------------------------------------------------------------------ */

typedef struct {
	uint8_t  src_addr[6];     /* transmitter address (addr2) */
	uint8_t  subtype;         /* AWDL_SUBTYPE_PSF or _MIF */

	bool     has_name;
	char     name[AWDL_NAME_MAX + 1];

	bool     has_version;
	uint8_t  version;
	uint8_t  devclass;

	bool     has_chanseq;
	uint8_t  chanseq_enc;     /* enum awdl_chan_enc of the decoded sequence */
	uint8_t  chanseq[AWDL_CHANSEQ_LEN]; /* decoded channel NUMBERS, 0 = absent */

	bool     has_election;
	uint8_t  master_addr[6];  /* currently elected master (v2 if present, else v1) */
	uint8_t  sync_addr[6];    /* v2 only: the node the peer syncs to (else zeroed) */
	uint32_t el_height;       /* distance to master (sync-tree height) */
	uint32_t el_master_metric;
	uint32_t el_master_counter; /* 0 for v1 (no counter field) */
	uint32_t el_self_metric;
	uint32_t el_self_counter;   /* 0 for v1 */

	bool     has_sync;        /* synchronization-parameters TLV present */
	uint16_t sync_tx_down_tu; /* TU until the sender's next AW (tx_down_counter) */
	uint16_t sync_aw_counter; /* the sender's current AW counter (next_aw_seq) */
} awdl_peer_view_t;

/* ------------------------------------------------------------------------ */
/* Building                                                                 */
/* ------------------------------------------------------------------------ */

/*
 * Build a complete AWDL action frame (PSF or MIF) starting at the 802.11 MAC
 * header, ready to hand to esp_wifi_80211_tx() (no radiotap, no FCS). The
 * destination is the AWDL broadcast target. now_us fills the action header's
 * phy_tx/target_tx timestamps; sync_tx_down_tu (TU until our next AW) and
 * sync_aw_counter (our current AW counter) fill the synchronization-parameters
 * TLV and must come from our AW clock (see awdl_sync.h / awdl_node.h), so the
 * timing we advertise matches the master we have adopted.
 *
 * Returns the number of bytes written, or a negative enum awdl_result.
 */
int awdl_build_action(uint8_t *buf, size_t cap, awdl_self_t *self, uint8_t subtype,
                      uint64_t now_us, uint16_t sync_tx_down_tu, uint16_t sync_aw_counter);

/*
 * Build a complete AWDL data frame carrying an IPv6 payload: 802.11 data header
 * + LLC/SNAP + AWDL data header + payload. dst is the destination AWDL MAC.
 * Increments self->seq_num and self->ieee_seq_num.
 *
 * Returns bytes written, or a negative enum awdl_result.
 */
int awdl_build_data(uint8_t *buf, size_t cap, awdl_self_t *self,
                    const uint8_t dst[6], const uint8_t *payload, size_t plen);

/* ------------------------------------------------------------------------ */
/* Parsing                                                                  */
/* ------------------------------------------------------------------------ */

/*
 * Parse a received AWDL action frame (802.11 MAC header onward, FCS already
 * stripped) into out. Fills the has_* flags for the TLVs it recognises. Returns
 * AWDL_OK, or a negative enum awdl_result if the frame is not a well-formed
 * AWDL action frame.
 */
int awdl_parse_action(const uint8_t *buf, size_t len, awdl_peer_view_t *out);

/*
 * True if the frame looks like an AWDL data frame (802.11 data, AWDL LLC/SNAP).
 * Accepts both plain data and QoS-data subtypes and ignores the 802.11 flag
 * bits, so real Apple frames parse. On success, *payload_off is the offset of
 * the L3 payload and *ethertype the carried ethertype (usually ETHERTYPE_IPV6).
 */
bool awdl_parse_data(const uint8_t *buf, size_t len,
                     size_t *payload_off, uint16_t *ethertype);

/*
 * 802.11 header length of a data frame that carries a body we handle (plain
 * data 24 B, QoS data 26 B), or 0 for anything else (management, null data,
 * QoS null, ...). *amsdu (optional) is set when the QoS "A-MSDU present" bit is
 * set, i.e. the body is a sequence of subframes rather than one MSDU.
 */
size_t awdl_data_hdr_len(const uint8_t *buf, size_t len, bool *amsdu);

/*
 * Called once per MSDU found in a data frame, reconstructed as Ethernet: dst/src
 * are addr1/addr2 for a plain frame or the subframe DA/SA for an A-MSDU; payload
 * is the L3 packet carried after the AWDL data header. Pointers are only valid
 * during the call.
 */
typedef void (*awdl_msdu_cb_t)(const uint8_t dst[6], const uint8_t src[6],
                               uint16_t ethertype, const uint8_t *payload,
                               size_t plen, void *ctx);

/*
 * Parse an AWDL data frame, plain or A-MSDU, and call cb (may be NULL) for each
 * MSDU. Returns the number of MSDUs delivered; 0 if the frame carries no body we
 * handle (see awdl_data_hdr_len); or a negative enum awdl_result if it is
 * malformed (subframes before the bad one have already been delivered).
 */
int awdl_parse_data_msdus(const uint8_t *buf, size_t len, awdl_msdu_cb_t cb, void *ctx);

/*
 * Offset just past the last complete TLV of an AWDL action frame, walking the
 * TLV chain from the action header over at most len bytes; 0 if buf is not an
 * AWDL action frame. With len a little larger than the frame, this shows where
 * the frame really ends (used to find out how a chip reports frame lengths).
 */
size_t awdl_action_tlv_end(const uint8_t *buf, size_t len);

/* Map an operating-class-encoded or legacy channel pair to a channel number. */
uint8_t awdl_chan_number(const uint8_t enc_bytes[2], enum awdl_chan_enc enc);

#ifdef __cplusplus
}
#endif

#endif /* ESPDROP_AWDL_FRAME_H */
