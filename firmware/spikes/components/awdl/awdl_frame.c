/*
 * EspDrop - AWDL frame construction and parsing (platform independent).
 *
 * See awdl_frame.h for provenance (OWL / The Open Wireless Link Project, Milan
 * Stute, GPL-3.0) and conventions.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Copyright (C) 2026  EspDrop contributors
 */
#include "awdl_frame.h"
#include "awdl_wire.h"

#include <string.h>

const uint8_t AWDL_BSSID[6]     = {0x00, 0x25, 0x00, 0xff, 0x94, 0x73};
const uint8_t AWDL_OUI[3]       = {0x00, 0x17, 0xf2};
const uint8_t AWDL_BROADCAST[6] = {0xff, 0xff, 0xff, 0xff, 0xff, 0xff};

/* ------------------------------------------------------------------------ */
/* Small internal helpers                                                   */
/* ------------------------------------------------------------------------ */

static int chan_enc_len(uint8_t enc) {
	return (enc == AWDL_CHAN_ENC_SIMPLE) ? 1 : 2;
}

uint8_t awdl_chan_number(const uint8_t enc_bytes[2], enum awdl_chan_enc enc) {
	switch (enc) {
	case AWDL_CHAN_ENC_SIMPLE:  return enc_bytes[0];
	case AWDL_CHAN_ENC_LEGACY:  return enc_bytes[1];
	case AWDL_CHAN_ENC_OPCLASS: return enc_bytes[0];
	default:                    return 0;
	}
}

/* Begin a TLV: write type + placeholder length, remember header offset. */
static bool tlv_begin(uint8_t *buf, size_t cap, size_t *off, size_t *hdr, uint8_t type) {
	*hdr = *off;
	return wire_put_u8(buf, cap, off, type) && wire_put_le16(buf, cap, off, 0);
}

/* Finish a TLV: backpatch length = bytes written after the 3-byte TL header. */
static void tlv_end(uint8_t *buf, size_t *off, size_t hdr) {
	uint16_t body = (uint16_t)(*off - hdr - 3);
	buf[hdr + 1] = (uint8_t)(body & 0xff);
	buf[hdr + 2] = (uint8_t)((body >> 8) & 0xff);
}

/* Write an awdl_chanseq descriptor followed by the 16 encoded slot entries. */
static bool put_chanseq(uint8_t *buf, size_t cap, size_t *off, const awdl_self_t *self) {
	int enc_len = chan_enc_len(self->chan_enc);
	bool ok = true;
	ok &= wire_put_u8(buf, cap, off, AWDL_CHANSEQ_LEN - 1); /* count (+1) */
	ok &= wire_put_u8(buf, cap, off, self->chan_enc);       /* encoding */
	ok &= wire_put_u8(buf, cap, off, 0);                    /* duplicate_count */
	ok &= wire_put_u8(buf, cap, off, AWDL_PRESENCE_MODE - 1); /* step_count (+1) */
	ok &= wire_put_le16(buf, cap, off, 0xffff);             /* fill_channel: repeat */
	for (unsigned i = 0; i < AWDL_CHANSEQ_LEN; i++)
		ok &= wire_put_bytes(buf, cap, off, self->seq[i], (size_t)enc_len);
	return ok;
}

/* ------------------------------------------------------------------------ */
/* Self state                                                               */
/* ------------------------------------------------------------------------ */

void awdl_self_init(awdl_self_t *self, const uint8_t self_addr[6],
                    const char *name, uint8_t chan_num) {
	memset(self, 0, sizeof(*self));
	memcpy(self->self_addr, self_addr, 6);

	size_t n = 0;
	if (name) {
		n = strlen(name);
		if (n > AWDL_NAME_MAX) n = AWDL_NAME_MAX;
		memcpy(self->name, name, n);
	}
	self->name[n] = '\0';

	self->version  = AWDL_VERSION_OWL;
	self->devclass = AWDL_DEVCLASS_MACOS;
	self->chan_enc = AWDL_CHAN_ENC_OPCLASS;
	self->master_chan = chan_num;

	uint8_t opclass = (chan_num <= 14) ? 0x51 : 0x80;
	for (unsigned i = 0; i < AWDL_CHANSEQ_LEN; i++) {
		self->seq[i][0] = chan_num;
		self->seq[i][1] = opclass;
	}
}

/* ------------------------------------------------------------------------ */
/* 802.11 header                                                            */
/* ------------------------------------------------------------------------ */

static bool put_dot11_hdr(uint8_t *buf, size_t cap, size_t *off, uint16_t fc,
                          const uint8_t dst[6], const uint8_t src[6], uint16_t ieee_seq) {
	bool ok = true;
	ok &= wire_put_le16(buf, cap, off, fc);
	ok &= wire_put_le16(buf, cap, off, 0); /* duration */
	ok &= wire_put_bytes(buf, cap, off, dst, 6);         /* addr1 */
	ok &= wire_put_bytes(buf, cap, off, src, 6);         /* addr2 */
	ok &= wire_put_bytes(buf, cap, off, AWDL_BSSID, 6);  /* addr3 */
	ok &= wire_put_le16(buf, cap, off, (uint16_t)(ieee_seq << 4)); /* seq ctrl */
	return ok;
}

/* ------------------------------------------------------------------------ */
/* Action frame TLV builders                                                */
/* ------------------------------------------------------------------------ */

static bool put_tlv_sync_params(uint8_t *buf, size_t cap, size_t *off,
                                const awdl_self_t *self, uint64_t now_us) {
	/* Free-running AW clock with epoch 0 (spike: not yet synced to a master). */
	uint64_t since_tu   = now_us / AWDL_TU_US;
	uint32_t eaw        = AWDL_EAW_TU; /* 64 */
	uint16_t tx_down    = (uint16_t)(eaw - (since_tu % eaw));          /* TU to next AW */
	uint16_t cur_aw     = (uint16_t)(((since_tu % eaw) / AWDL_AW_PERIOD_TU) +
	                                 AWDL_PRESENCE_MODE * (since_tu / eaw));
	uint16_t aw_com     = AWDL_AW_PERIOD_TU;
	uint16_t consumed   = (uint16_t)(AWDL_AW_PERIOD_TU * AWDL_PRESENCE_MODE - tx_down);
	uint16_t remaining  = (aw_com < consumed) ? 0 : (uint16_t)(aw_com - consumed);

	size_t hdr;
	bool ok = tlv_begin(buf, cap, off, &hdr, AWDL_TLV_SYNC_PARAMS);
	ok &= wire_put_u8(buf, cap, off, self->master_chan);   /* next_aw_channel */
	ok &= wire_put_le16(buf, cap, off, tx_down);           /* tx_down_counter */
	ok &= wire_put_u8(buf, cap, off, self->master_chan);   /* master_channel */
	ok &= wire_put_u8(buf, cap, off, 0);                   /* guard_time */
	ok &= wire_put_le16(buf, cap, off, AWDL_AW_PERIOD_TU); /* aw_period */
	ok &= wire_put_le16(buf, cap, off, AWDL_PSF_INTERVAL_TU); /* af_period */
	ok &= wire_put_le16(buf, cap, off, 0x1800);            /* flags */
	ok &= wire_put_le16(buf, cap, off, AWDL_AW_PERIOD_TU); /* aw_ext_length */
	ok &= wire_put_le16(buf, cap, off, aw_com);            /* aw_com_length */
	ok &= wire_put_le16(buf, cap, off, remaining);         /* remaining_aw_length */
	ok &= wire_put_u8(buf, cap, off, AWDL_PRESENCE_MODE - 1); /* min_ext */
	ok &= wire_put_u8(buf, cap, off, AWDL_PRESENCE_MODE - 1); /* max_ext_multicast */
	ok &= wire_put_u8(buf, cap, off, AWDL_PRESENCE_MODE - 1); /* max_ext_unicast */
	ok &= wire_put_u8(buf, cap, off, AWDL_PRESENCE_MODE - 1); /* max_ext_af */
	ok &= wire_put_bytes(buf, cap, off, self->self_addr, 6); /* master_addr (=self) */
	ok &= wire_put_u8(buf, cap, off, AWDL_PRESENCE_MODE);  /* presence_mode */
	ok &= wire_put_u8(buf, cap, off, 0);                   /* reserved */
	ok &= wire_put_le16(buf, cap, off, cur_aw);            /* next_aw_seq */
	ok &= wire_put_le16(buf, cap, off, cur_aw);            /* ap_alignment */
	ok &= put_chanseq(buf, cap, off, self);
	ok &= wire_put_le16(buf, cap, off, 0);                 /* padding */
	tlv_end(buf, off, hdr);
	return ok;
}

static bool put_tlv_election_params(uint8_t *buf, size_t cap, size_t *off,
                                    const awdl_self_t *self) {
	size_t hdr;
	bool ok = tlv_begin(buf, cap, off, &hdr, AWDL_TLV_ELECTION_PARAMS);
	ok &= wire_put_u8(buf, cap, off, 0);                   /* flags */
	ok &= wire_put_le16(buf, cap, off, 0);                 /* id */
	ok &= wire_put_u8(buf, cap, off, 0);                   /* distancetop (height) */
	ok &= wire_put_u8(buf, cap, off, 0);                   /* unknown */
	ok &= wire_put_bytes(buf, cap, off, self->self_addr, 6); /* top_master_addr */
	ok &= wire_put_le32(buf, cap, off, AWDL_ELECTION_METRIC_INIT); /* top_master_metric */
	ok &= wire_put_le32(buf, cap, off, AWDL_ELECTION_METRIC_INIT); /* self_metric */
	ok &= wire_put_le16(buf, cap, off, 0);                 /* pad */
	tlv_end(buf, off, hdr);
	return ok;
}

static bool put_tlv_chanseq(uint8_t *buf, size_t cap, size_t *off, const awdl_self_t *self) {
	size_t hdr;
	bool ok = tlv_begin(buf, cap, off, &hdr, AWDL_TLV_CHAN_SEQ);
	ok &= put_chanseq(buf, cap, off, self);
	ok &= wire_put_u8(buf, cap, off, 0);
	ok &= wire_put_u8(buf, cap, off, 0);
	ok &= wire_put_u8(buf, cap, off, 0); /* padding (3 bytes, as OWL) */
	tlv_end(buf, off, hdr);
	return ok;
}

static bool put_tlv_election_params_v2(uint8_t *buf, size_t cap, size_t *off,
                                       const awdl_self_t *self) {
	size_t hdr;
	bool ok = tlv_begin(buf, cap, off, &hdr, AWDL_TLV_ELECTION_PARAMS_V2);
	ok &= wire_put_bytes(buf, cap, off, self->self_addr, 6); /* master_addr */
	ok &= wire_put_bytes(buf, cap, off, self->self_addr, 6); /* sync_addr */
	ok &= wire_put_le32(buf, cap, off, 0);                 /* master_counter */
	ok &= wire_put_le32(buf, cap, off, 0);                 /* distance_to_master */
	ok &= wire_put_le32(buf, cap, off, AWDL_ELECTION_METRIC_INIT); /* master_metric */
	ok &= wire_put_le32(buf, cap, off, AWDL_ELECTION_METRIC_INIT); /* self_metric */
	ok &= wire_put_le32(buf, cap, off, 0);                 /* unknown */
	ok &= wire_put_le32(buf, cap, off, 0);                 /* reserved */
	ok &= wire_put_le32(buf, cap, off, 0);                 /* self_counter */
	tlv_end(buf, off, hdr);
	return ok;
}

static bool put_tlv_service_params(uint8_t *buf, size_t cap, size_t *off) {
	size_t hdr;
	bool ok = tlv_begin(buf, cap, off, &hdr, AWDL_TLV_SERVICE_PARAMS);
	ok &= wire_put_u8(buf, cap, off, 0);
	ok &= wire_put_u8(buf, cap, off, 0);
	ok &= wire_put_u8(buf, cap, off, 0);   /* unknown[3] */
	ok &= wire_put_le16(buf, cap, off, 0); /* sui */
	ok &= wire_put_le32(buf, cap, off, 0); /* bitmask */
	tlv_end(buf, off, hdr);
	return ok;
}

static bool put_tlv_ht_capabilities(uint8_t *buf, size_t cap, size_t *off) {
	size_t hdr;
	bool ok = tlv_begin(buf, cap, off, &hdr, AWDL_TLV_HT_CAPABILITIES);
	ok &= wire_put_le16(buf, cap, off, 0);      /* unknown */
	ok &= wire_put_le16(buf, cap, off, 0x11ce); /* ht_capabilities */
	ok &= wire_put_u8(buf, cap, off, 0x1b);     /* ampdu_params */
	ok &= wire_put_u8(buf, cap, off, 0xff);     /* rx_mcs: one stream, all MCS */
	ok &= wire_put_le16(buf, cap, off, 0);      /* unknown2 */
	tlv_end(buf, off, hdr);
	return ok;
}

static bool put_tlv_arpa(uint8_t *buf, size_t cap, size_t *off, const awdl_self_t *self) {
	size_t hdr;
	size_t nlen = strlen(self->name);
	bool ok = tlv_begin(buf, cap, off, &hdr, AWDL_TLV_ARPA);
	ok &= wire_put_u8(buf, cap, off, 3);                 /* flags */
	ok &= wire_put_u8(buf, cap, off, (uint8_t)nlen);     /* name_length */
	ok &= wire_put_bytes(buf, cap, off, self->name, nlen);
	ok &= wire_put_be16(buf, cap, off, 0xc00c);          /* compressed ".local" */
	tlv_end(buf, off, hdr);
	return ok;
}

static bool put_tlv_data_path_state(uint8_t *buf, size_t cap, size_t *off,
                                    const awdl_self_t *self) {
	uint16_t social;
	switch (self->master_chan) {
	case 6:   social = 0x0001; break;
	case 44:  social = 0x0002; break;
	default:  social = 0x0004; break; /* 149 */
	}
	size_t hdr;
	bool ok = tlv_begin(buf, cap, off, &hdr, AWDL_TLV_DATA_PATH_STATE);
	ok &= wire_put_le16(buf, cap, off, AWDL_DPS_FLAGS_OWL); /* flags */
	ok &= wire_put_u8(buf, cap, off, 'X');
	ok &= wire_put_u8(buf, cap, off, '0');
	ok &= wire_put_u8(buf, cap, off, 0);                   /* country_code[3] */
	ok &= wire_put_le16(buf, cap, off, social);            /* social_channels */
	ok &= wire_put_bytes(buf, cap, off, self->self_addr, 6); /* awdl_addr */
	ok &= wire_put_le16(buf, cap, off, 0);                 /* ext_flags */
	tlv_end(buf, off, hdr);
	return ok;
}

static bool put_tlv_version(uint8_t *buf, size_t cap, size_t *off, const awdl_self_t *self) {
	size_t hdr;
	bool ok = tlv_begin(buf, cap, off, &hdr, AWDL_TLV_VERSION);
	ok &= wire_put_u8(buf, cap, off, self->version);
	ok &= wire_put_u8(buf, cap, off, self->devclass);
	tlv_end(buf, off, hdr);
	return ok;
}

/* ------------------------------------------------------------------------ */
/* Public builders                                                          */
/* ------------------------------------------------------------------------ */

int awdl_build_action(uint8_t *buf, size_t cap, awdl_self_t *self,
                      uint8_t subtype, uint64_t now_us) {
	if (!buf || !self) return AWDL_ERR_ARG;

	size_t off = 0;
	bool ok = put_dot11_hdr(buf, cap, &off, FC_ACTION, AWDL_BROADCAST,
	                        self->self_addr, self->ieee_seq_num);
	self->ieee_seq_num = (self->ieee_seq_num + 1) & 0x0fff;

	/* AWDL action header */
	ok &= wire_put_u8(buf, cap, &off, AWDL_CATEGORY_VENDOR);
	ok &= wire_put_bytes(buf, cap, &off, AWDL_OUI, 3);
	ok &= wire_put_u8(buf, cap, &off, AWDL_ACTION_TYPE);
	ok &= wire_put_u8(buf, cap, &off, AWDL_ACTION_VERSION);
	ok &= wire_put_u8(buf, cap, &off, subtype);
	ok &= wire_put_u8(buf, cap, &off, 0); /* reserved */
	ok &= wire_put_le32(buf, cap, &off, (uint32_t)now_us); /* phy_tx */
	ok &= wire_put_le32(buf, cap, &off, (uint32_t)now_us); /* target_tx */

	ok &= put_tlv_sync_params(buf, cap, &off, self, now_us);
	ok &= put_tlv_election_params(buf, cap, &off, self);
	ok &= put_tlv_chanseq(buf, cap, &off, self);
	ok &= put_tlv_election_params_v2(buf, cap, &off, self);
	ok &= put_tlv_service_params(buf, cap, &off);
	if (subtype == AWDL_SUBTYPE_MIF) {
		ok &= put_tlv_ht_capabilities(buf, cap, &off);
		ok &= put_tlv_arpa(buf, cap, &off, self);
	}
	ok &= put_tlv_data_path_state(buf, cap, &off, self);
	ok &= put_tlv_version(buf, cap, &off, self);

	if (!ok) return AWDL_ERR_ARG; /* ran out of buffer */
	return (int)off;
}

int awdl_build_data(uint8_t *buf, size_t cap, awdl_self_t *self,
                    const uint8_t dst[6], const uint8_t *payload, size_t plen) {
	if (!buf || !self || !dst || (!payload && plen)) return AWDL_ERR_ARG;

	size_t off = 0;
	bool ok = put_dot11_hdr(buf, cap, &off, FC_DATA, dst, self->self_addr,
	                        self->ieee_seq_num);
	self->ieee_seq_num = (self->ieee_seq_num + 1) & 0x0fff;

	/* LLC / SNAP */
	ok &= wire_put_u8(buf, cap, &off, 0xaa);
	ok &= wire_put_u8(buf, cap, &off, 0xaa);
	ok &= wire_put_u8(buf, cap, &off, 0x03);
	ok &= wire_put_bytes(buf, cap, &off, AWDL_OUI, 3);
	ok &= wire_put_be16(buf, cap, &off, AWDL_LLC_PID);

	/* AWDL data header */
	ok &= wire_put_le16(buf, cap, &off, AWDL_DATA_HEAD);
	ok &= wire_put_le16(buf, cap, &off, self->seq_num++);
	ok &= wire_put_le16(buf, cap, &off, 0); /* pad */
	ok &= wire_put_be16(buf, cap, &off, ETHERTYPE_IPV6);

	ok &= wire_put_bytes(buf, cap, &off, payload, plen);

	if (!ok) return AWDL_ERR_ARG;
	return (int)off;
}

/* ------------------------------------------------------------------------ */
/* Parsing                                                                  */
/* ------------------------------------------------------------------------ */

static void parse_chanseq_tlv(const uint8_t *v, size_t vlen, awdl_peer_view_t *out) {
	uint8_t enc;
	if (!wire_get_u8(v, vlen, 1, &enc)) return;
	int elen = chan_enc_len(enc);
	size_t need = 6 + (size_t)elen * AWDL_CHANSEQ_LEN;
	if (vlen < need) return;
	out->chanseq_enc = enc;
	for (unsigned i = 0; i < AWDL_CHANSEQ_LEN; i++) {
		uint8_t eb[2] = {0, 0};
		wire_get_bytes(v, vlen, 6 + (size_t)i * elen, eb, (size_t)elen);
		out->chanseq[i] = awdl_chan_number(eb, (enum awdl_chan_enc)enc);
	}
	out->has_chanseq = true;
}

int awdl_parse_action(const uint8_t *buf, size_t len, awdl_peer_view_t *out) {
	if (!buf || !out) return AWDL_ERR_ARG;
	memset(out, 0, sizeof(*out));

	if (len < IEEE80211_HDR_LEN + 16) return AWDL_ERR_SHORT;

	/* transmitter = addr2 */
	wire_get_bytes(buf, len, 10, out->src_addr, 6);

	/* AWDL action header at offset 24 */
	size_t a = IEEE80211_HDR_LEN;
	uint8_t category, type;
	wire_get_u8(buf, len, a + 0, &category);
	if (category != AWDL_CATEGORY_VENDOR) return AWDL_ERR_NOT_AWDL;
	if (memcmp(buf + a + 1, AWDL_OUI, 3) != 0) return AWDL_ERR_NOT_AWDL;
	wire_get_u8(buf, len, a + 4, &type);
	if (type != AWDL_ACTION_TYPE) return AWDL_ERR_NOT_AWDL;
	wire_get_u8(buf, len, a + 6, &out->subtype);

	/* TLVs start after the 16-byte action header */
	size_t off = a + 16;
	bool election_v2 = false;
	while (off + 3 <= len) {
		uint8_t  t = buf[off];
		uint16_t l = (uint16_t)(buf[off + 1] | ((uint16_t)buf[off + 2] << 8));
		const uint8_t *v = buf + off + 3;
		if (off + 3 + l > len) break; /* truncated TLV */

		switch (t) {
		case AWDL_TLV_CHAN_SEQ:
			parse_chanseq_tlv(v, l, out);
			break;
		case AWDL_TLV_ARPA: {
			uint8_t nlen;
			if (wire_get_u8(v, l, 1, &nlen)) {
				size_t cpy = nlen;
				if (cpy > AWDL_NAME_MAX) cpy = AWDL_NAME_MAX;
				if (2 + cpy <= l) {
					memcpy(out->name, v + 2, cpy);
					out->name[cpy] = '\0';
					out->has_name = true;
				}
			}
			break;
		}
		case AWDL_TLV_VERSION:
			if (wire_get_u8(v, l, 0, &out->version) &&
			    wire_get_u8(v, l, 1, &out->devclass))
				out->has_version = true;
			break;
		case AWDL_TLV_ELECTION_PARAMS_V2:
			if (wire_get_bytes(v, l, 0, out->master_addr, 6)) {
				out->has_election = true;
				election_v2 = true;
			}
			break;
		case AWDL_TLV_ELECTION_PARAMS:
			/* v1: top_master_addr at offset 5; ignored if v2 already seen */
			if (!election_v2 && wire_get_bytes(v, l, 5, out->master_addr, 6))
				out->has_election = true;
			break;
		default:
			break;
		}
		off += 3 + l;
	}
	return AWDL_OK;
}

bool awdl_parse_data(const uint8_t *buf, size_t len,
                     size_t *payload_off, uint16_t *ethertype) {
	if (!buf || len < IEEE80211_HDR_LEN + 8 + 8) return false;

	uint16_t fc;
	wire_get_le16(buf, len, 0, &fc);
	if (fc != FC_DATA) return false;

	size_t llc = IEEE80211_HDR_LEN;
	uint8_t dsap, ssap, ctrl;
	wire_get_u8(buf, len, llc + 0, &dsap);
	wire_get_u8(buf, len, llc + 1, &ssap);
	wire_get_u8(buf, len, llc + 2, &ctrl);
	if (dsap != 0xaa || ssap != 0xaa || ctrl != 0x03) return false;
	if (memcmp(buf + llc + 3, AWDL_OUI, 3) != 0) return false;

	uint16_t pid, head, et;
	wire_get_be16(buf, len, llc + 6, &pid);
	if (pid != AWDL_LLC_PID) return false;

	size_t adata = llc + 8;
	wire_get_le16(buf, len, adata + 0, &head);
	if (head != AWDL_DATA_HEAD) return false;
	wire_get_be16(buf, len, adata + 6, &et);

	if (ethertype) *ethertype = et;
	if (payload_off) *payload_off = adata + 8;
	return true;
}
