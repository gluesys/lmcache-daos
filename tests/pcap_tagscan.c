/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Gluesys Co., Ltd. */
/*
 * pcap_tagscan.c -- count tagged-payload bytes per (tid, round, chunk) in a
 * tcpdump capture, so the wire itself testifies in the corruption fault split.
 *
 * The reproducers fill every 8 bytes of payload with a self-describing tag,
 *     (tid << 56) | (round << 48) | absolute_payload_offset,
 * so any TCP segment that carries object data can be attributed to its source
 * object, generation and offset without knowing anything about the RPCs
 * around it. Under provider ofi+tcp a DAOS fetch response is plain TCP from
 * the server, which turns tcpdump into a probe between the server's send path
 * and the client's receive path:
 *
 *   - the wrong chunk's bytes ARE on the wire (its (tid,chunk) counted more
 *     times than the workload model expects, the victim's chunk short)
 *         -> the server sent wrong data: server-side fetch/bulk fault
 *   - the wire counts match the model exactly, yet the application buffer
 *     held another object's chunk
 *         -> the client scattered it wrong: client/mercury receive fault
 *
 * Scanning is content-based: at every byte offset, an 8-byte little-endian
 * word is a tag candidate if tid < MAXTID, round < MAXROUND, offset < MAXOFF,
 * offset % 8 == 0. A candidate only counts once it extends into a run
 * (successive words each +8), and runs shorter than MINRUN bytes are dropped,
 * so random bytes practically never qualify. Runs are split by segment
 * boundaries; the per-boundary loss is at most one word, which is noise at
 * 4 MiB chunk granularity.
 *
 *   ./pcap_tagscan <capture.pcap> [chunk_MiB (default 4)]
 *
 * Output: one line per (direction, tid, round, chunk) with byte totals, then
 * per-(tid,round) object totals. Direction is by IPv4 source address:
 * 203.0.113.82/.84 (the cells' data interfaces) count as S->C, anything
 * else as C->S.
 */
#include <arpa/inet.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAXTID   16
#define MAXROUND 64
#define MAXOFF   (64ull << 20)
#define MINRUN   512

static uint32_t srv_ips[2];
static size_t   g_chunk = 4ull << 20;

/* (dir, tid, round, chunk) -> bytes.  dir 0 = S->C, 1 = C->S. */
#define MAXCHUNK 16
static uint64_t agg[2][MAXTID][MAXROUND][MAXCHUNK];

static inline bool tag_ok(uint64_t v)
{
	return (v >> 56) < MAXTID &&
	       ((v >> 48) & 0xff) < MAXROUND &&
	       (v & 0x0000ffffffffffffULL) < MAXOFF &&
	       (v & 7) == 0;
}

static void scan_payload(const unsigned char *p, size_t n, int dir)
{
	size_t i = 0;

	while (i + 8 <= n) {
		uint64_t v;

		memcpy(&v, p + i, 8);
		if (!tag_ok(v)) {
			i++;
			continue;
		}
		/* extend the run */
		size_t start = i;
		uint64_t w = v;

		while (i + 16 <= n) {
			uint64_t nx;

			memcpy(&nx, p + i + 8, 8);
			if (nx != w + 8)
				break;
			i += 8;
			w = nx;
		}
		size_t runlen = i + 8 - start;

		if (runlen >= MINRUN) {
			int tid = (int)(v >> 56);
			int rnd = (int)((v >> 48) & 0xff);
			uint64_t off = v & 0x0000ffffffffffffULL;
			int chunk = (int)(off / g_chunk);

			if (chunk < MAXCHUNK)
				agg[dir][tid][rnd][chunk] += runlen;
			i += 8;
		} else {
			i = start + 1;
		}
	}
}

int main(int argc, char **argv)
{
	if (argc < 2) {
		fprintf(stderr, "usage: %s <capture.pcap> [chunk_MiB]\n", argv[0]);
		return 2;
	}
	if (argc > 2)
		g_chunk = (size_t)strtoul(argv[2], NULL, 0) << 20;

	srv_ips[0] = inet_addr("203.0.113.82");
	srv_ips[1] = inet_addr("203.0.113.84");

	FILE *f = fopen(argv[1], "rb");
	if (!f) { perror(argv[1]); return 2; }

	unsigned char gh[24];
	if (fread(gh, 1, 24, f) != 24) { fprintf(stderr, "short header\n"); return 2; }
	uint32_t magic;
	memcpy(&magic, gh, 4);
	bool swap;
	if (magic == 0xa1b2c3d4 || magic == 0xa1b23c4d)      swap = false;
	else if (magic == 0xd4c3b2a1 || magic == 0x4d3cb2a1) swap = true;
	else { fprintf(stderr, "not classic pcap (magic %08x)\n", magic); return 2; }

	unsigned char *buf = malloc(1 << 20);
	unsigned long pkts = 0, tagged = 0;

	for (;;) {
		unsigned char ph[16];
		if (fread(ph, 1, 16, f) != 16)
			break;
		uint32_t caplen;
		memcpy(&caplen, ph + 8, 4);
		if (swap)
			caplen = __builtin_bswap32(caplen);
		if (caplen > (1u << 20)) { fprintf(stderr, "caplen %u?\n", caplen); break; }
		if (fread(buf, 1, caplen, f) != caplen)
			break;
		pkts++;

		/* ethernet */
		if (caplen < 34)
			continue;
		size_t l3 = 14;
		uint16_t etype = (buf[12] << 8) | buf[13];
		if (etype == 0x8100) { etype = (buf[16] << 8) | buf[17]; l3 = 18; }
		if (etype != 0x0800)
			continue;
		/* ipv4 */
		if ((buf[l3] >> 4) != 4)
			continue;
		size_t ihl = (buf[l3] & 0xf) * 4;
		if (buf[l3 + 9] != 6)          /* TCP */
			continue;
		uint32_t sip;
		memcpy(&sip, buf + l3 + 12, 4);
		size_t l4 = l3 + ihl;
		if (l4 + 20 > caplen)
			continue;
		size_t doff = (buf[l4 + 12] >> 4) * 4;
		size_t pay = l4 + doff;
		if (pay >= caplen)
			continue;

		int dir = (sip == srv_ips[0] || sip == srv_ips[1]) ? 0 : 1;
		size_t before = 0;
		(void)before;
		scan_payload(buf + pay, caplen - pay, dir);
		tagged++;
	}
	fclose(f);
	free(buf);

	fprintf(stderr, "packets=%lu parsed-with-payload=%lu\n", pkts, tagged);

	static const char *dn[2] = { "S->C", "C->S" };
	for (int d = 0; d < 2; d++)
		for (int t = 0; t < MAXTID; t++)
			for (int r = 0; r < MAXROUND; r++) {
				uint64_t tot = 0;
				for (int c = 0; c < MAXCHUNK; c++)
					tot += agg[d][t][r][c];
				if (!tot)
					continue;
				printf("%s t%-2d r%-2d total=%8.2fMiB |", dn[d], t, r,
				       tot / 1048576.0);
				for (int c = 0; c < MAXCHUNK; c++)
					if (agg[d][t][r][c])
						printf(" c%d=%.2fM", c,
						       agg[d][t][r][c] / 1048576.0);
				printf("\n");
			}
	return 0;
}
