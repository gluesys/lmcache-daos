/*
 * spdk_nvme_tagio.c -- DAOS-free minimal reproducer for the silent read
 * corruption, straight on SPDK's userspace NVMe driver.
 *
 * Why this exists: section 27 of DAOS-CONCURRENT-READ-CORRUPTION.md narrowed
 * the fault to "SPDK userspace driver DMAing to a real NVMe controller through
 * VFIO/IOMMU" -- class:file on the same host, same DAOS build and same config
 * is completely clean, while class:nvme on real drives corrupts up to 24% of
 * reads. Everything above the driver (DAOS object/VOS/bio/blobstore, the
 * transport, the client) has been excluded by measurement. This program removes
 * DAOS entirely: it writes tagged 4 MiB regions directly to the raw namespace
 * over several queue pairs, reads them back concurrently, and verifies.
 *
 * If this corrupts, the report belongs to SPDK (or the drive firmware) and no
 * DAOS knowledge is needed to reproduce it. If it stays clean while DAOS on the
 * same device corrupts, the fault needs something DAOS's I/O pattern does that
 * this does not -- and the difference between the two becomes the next lead.
 *
 * Payload: every 8-byte word is (region << 48) | (round << 40) | byte_offset,
 * the same self-describing scheme as the DAOS-level reproducers, so a wrong
 * region names its own origin instead of just failing a memcmp.
 *
 * DESTRUCTIVE: writes to raw LBAs. Only run against a drive whose contents you
 * intend to destroy (i.e. a DAOS data device that is going to be reformatted).
 *
 *   ./spdk_nvme_tagio -a <pci_addr> [-q queues] [-r rounds] [-s region_MiB]
 *                     [-n regions] [-o lba_offset]
 *
 * Build (on cell1, against the DAOS-bundled SPDK build tree):
 *   B=/var/daosbuild/build-stockfull/external/release/spdk
 *   gcc -O2 -pthread -o spdk_nvme_tagio spdk_nvme_tagio.c \
 *       -I$B/include -I$B/dpdk/build/include \
 *       -Wl,--whole-archive $B/build/lib/libspdk_nvme.a \
 *          $B/build/lib/libspdk_env_dpdk.a $B/build/lib/libspdk_util.a \
 *          $B/build/lib/libspdk_log.a $B/build/lib/libspdk_json.a \
 *          $B/build/lib/libspdk_sock.a $B/build/lib/libspdk_trace.a \
 *          $B/build/lib/libspdk_rpc.a $B/build/lib/libspdk_jsonrpc.a \
 *          $B/build/lib/libspdk_vfio_user.a $B/build/lib/libspdk_keyring.a \
 *       -Wl,--no-whole-archive \
 *       -L$B/dpdk/build/lib -Wl,--whole-archive -ldpdk -Wl,--no-whole-archive \
 *       -lnuma -ldl -lrt -luuid -lssl -lcrypto -lm -lisal -larchive
 */
#define _GNU_SOURCE
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "spdk/env.h"
#include "spdk/nvme.h"
#include "spdk/stdinc.h"
#include "spdk/string.h"

#define SECTOR_FALLBACK 4096

static char    *g_pci;
static int      g_queues  = 8;
static int      g_rounds  = 20;
static size_t   g_region  = 4ul << 20;   /* one DAOS DFS chunk */
static int      g_regions = 16;          /* like 16 concurrent objects */
static uint64_t g_lba_off = 1048576;      /* stay clear of any label at LBA 0 */

static struct spdk_nvme_ctrlr *g_ctrlr;
static struct spdk_nvme_ns    *g_ns;
static uint32_t                g_sector;

static pthread_mutex_t g_log = PTHREAD_MUTEX_INITIALIZER;
static long            g_reads, g_bad;

/* ---- tagged payload (same scheme as the DAOS-level reproducers) ---------- */
static inline uint64_t tag_of(int region, int round, size_t off)
{
	return ((uint64_t)(region & 0xffff) << 48) |
	       ((uint64_t)(round & 0xff) << 40) |
	       (off & 0x000000ffffffffffULL);
}

static void fill_tagged(void *buf, int region, int round, size_t n)
{
	uint64_t *w = buf;

	for (size_t i = 0; i < n / 8; i++)
		w[i] = tag_of(region, round, i * 8);
}

/*
 * Report what the wrong bytes actually are: another region's data, a stale
 * round of our own, our own data from the wrong offset, or zeros. This is the
 * distinction that made the DAOS-level evidence usable.
 */
static void diagnose(const void *got, size_t n, int region, int round,
		     char *out, size_t outsz)
{
	const uint64_t *w = got;
	size_t n_ok = 0, n_zero = 0, n_reg = 0, n_round = 0, n_off = 0, n_junk = 0;
	size_t first = (size_t)-1;
	uint64_t fv = 0;

	for (size_t i = 0; i < n / 8; i++) {
		uint64_t want = tag_of(region, round, i * 8);

		if (w[i] == want) { n_ok++; continue; }
		if (first == (size_t)-1) { first = i * 8; fv = w[i]; }
		if (w[i] == 0) { n_zero++; continue; }

		int    r  = (int)(w[i] >> 48);
		int    rd = (int)((w[i] >> 40) & 0xff);
		size_t o  = w[i] & 0x000000ffffffffffULL;

		if (r != region && r < g_regions)      n_reg++;
		else if (r == region && rd != round)   n_round++;
		else if (r == region && o != i * 8)    n_off++;
		else                                   n_junk++;
	}
	snprintf(out, outsz,
		 "first bad at %zu: region=%d round=%d off=%zu | ok=%zu zero=%zu "
		 "foreign-region=%zu stale-round=%zu shifted=%zu junk=%zu",
		 first == (size_t)-1 ? 0 : first,
		 (int)(fv >> 48), (int)((fv >> 40) & 0xff),
		 (size_t)(fv & 0x000000ffffffffffULL),
		 n_ok, n_zero, n_reg, n_round, n_off, n_junk);
}

/* ---- SPDK plumbing ------------------------------------------------------ */
static bool probe_cb(void *cb_ctx, const struct spdk_nvme_transport_id *trid,
		     struct spdk_nvme_ctrlr_opts *opts)
{
	(void)cb_ctx; (void)opts;
	printf("attaching to %s\n", trid->traddr);
	return true;
}

static void attach_cb(void *cb_ctx, const struct spdk_nvme_transport_id *trid,
		      struct spdk_nvme_ctrlr *ctrlr,
		      const struct spdk_nvme_ctrlr_opts *opts)
{
	(void)cb_ctx; (void)trid; (void)opts;
	g_ctrlr = ctrlr;
	for (int nsid = spdk_nvme_ctrlr_get_first_active_ns(ctrlr); nsid != 0;
	     nsid = spdk_nvme_ctrlr_get_next_active_ns(ctrlr, nsid)) {
		struct spdk_nvme_ns *ns = spdk_nvme_ctrlr_get_ns(ctrlr, nsid);

		if (ns && spdk_nvme_ns_is_active(ns)) { g_ns = ns; break; }
	}
}

struct io_ctx {
	volatile int outstanding;
	volatile int errors;
};

static void io_complete(void *arg, const struct spdk_nvme_cpl *cpl)
{
	struct io_ctx *c = arg;

	if (spdk_nvme_cpl_is_error(cpl))
		c->errors++;
	c->outstanding--;
}

struct worker {
	int                     id;
	struct spdk_nvme_qpair *qp;
	void                   *wbuf;
	void                   *rbuf;
	int                     rc;
};

/*
 * Each worker owns its own queue pair and its own region, mirroring the DAOS
 * shape: many concurrent 4 MiB reads in flight against one controller. Writes
 * are issued first, then all workers read back and verify.
 */
static void *worker_fn(void *arg)
{
	struct worker *w = arg;
	struct io_ctx ctx;
	char detail[256];
	uint32_t sectors = g_region / g_sector;
	uint64_t lba = g_lba_off + (uint64_t)w->id * sectors;

	for (int round = 0; round < g_rounds; round++) {
		int region = w->id;

		/* write */
		fill_tagged(w->wbuf, region, round, g_region);
		ctx.outstanding = 1; ctx.errors = 0;
		if (spdk_nvme_ns_cmd_write(g_ns, w->qp, w->wbuf, lba, sectors,
					   io_complete, &ctx, 0) != 0) {
			w->rc = 1;
			return NULL;
		}
		while (ctx.outstanding)
			spdk_nvme_qpair_process_completions(w->qp, 0);
		if (ctx.errors) { w->rc = 2; return NULL; }

		/* read back, poison destination first */
		memset(w->rbuf, 0xA5, g_region);
		ctx.outstanding = 1; ctx.errors = 0;
		if (spdk_nvme_ns_cmd_read(g_ns, w->qp, w->rbuf, lba, sectors,
					  io_complete, &ctx, 0) != 0) {
			w->rc = 3;
			return NULL;
		}
		while (ctx.outstanding)
			spdk_nvme_qpair_process_completions(w->qp, 0);
		if (ctx.errors) { w->rc = 4; return NULL; }

		pthread_mutex_lock(&g_log);
		g_reads++;
		pthread_mutex_unlock(&g_log);

		if (memcmp(w->rbuf, w->wbuf, g_region) == 0)
			continue;

		diagnose(w->rbuf, g_region, region, round, detail, sizeof(detail));
		pthread_mutex_lock(&g_log);
		g_bad++;
		printf("  CORRUPT q%-2d r%-2d lba=%lu %s\n",
		       w->id, round, (unsigned long)lba, detail);
		fflush(stdout);
		pthread_mutex_unlock(&g_log);
	}
	return NULL;
}

int main(int argc, char **argv)
{
	struct spdk_env_opts opts;
	struct spdk_nvme_transport_id trid = {};
	int op;

	while ((op = getopt(argc, argv, "a:q:r:s:n:o:h")) != -1) {
		switch (op) {
		case 'a': g_pci = optarg; break;
		case 'q': g_queues = atoi(optarg); break;
		case 'r': g_rounds = atoi(optarg); break;
		case 's': g_region = (size_t)strtoul(optarg, NULL, 0) << 20; break;
		case 'n': g_regions = atoi(optarg); break;
		case 'o': g_lba_off = strtoull(optarg, NULL, 0); break;
		default:
			fprintf(stderr, "usage: %s -a <pci> [-q queues] "
				"[-r rounds] [-s MiB] [-n regions] [-o lba]\n",
				argv[0]);
			return 2;
		}
	}
	if (!g_pci) {
		fprintf(stderr, "-a <pci addr> is required (DESTRUCTIVE: raw writes)\n");
		return 2;
	}
	if (g_queues > g_regions)
		g_regions = g_queues;

	spdk_env_opts_init(&opts);
	opts.name = "spdk_nvme_tagio";
	opts.core_mask = "0x1";
	if (spdk_env_init(&opts) < 0) {
		fprintf(stderr, "spdk_env_init failed\n");
		return 2;
	}

	spdk_nvme_trid_populate_transport(&trid, SPDK_NVME_TRANSPORT_PCIE);
	snprintf(trid.traddr, sizeof(trid.traddr), "%s", g_pci);
	if (spdk_nvme_probe(&trid, NULL, probe_cb, attach_cb, NULL) != 0 ||
	    g_ctrlr == NULL || g_ns == NULL) {
		fprintf(stderr, "probe/attach failed for %s\n", g_pci);
		return 2;
	}

	g_sector = spdk_nvme_ns_get_sector_size(g_ns);
	if (g_sector == 0)
		g_sector = SECTOR_FALLBACK;
	if (g_region % g_sector) {
		fprintf(stderr, "region %zu not a multiple of sector %u\n",
			g_region, g_sector);
		return 2;
	}
	printf("device=%s sector=%u region=%zuMiB queues=%d rounds=%d lba_off=%lu\n",
	       g_pci, g_sector, g_region >> 20, g_queues, g_rounds,
	       (unsigned long)g_lba_off);

	struct worker *ws = calloc(g_queues, sizeof(*ws));
	pthread_t *th = calloc(g_queues, sizeof(*th));

	for (int i = 0; i < g_queues; i++) {
		ws[i].id = i;
		ws[i].qp = spdk_nvme_ctrlr_alloc_io_qpair(g_ctrlr, NULL, 0);
		if (!ws[i].qp) {
			fprintf(stderr, "qpair alloc failed at %d\n", i);
			return 2;
		}
		/* DMA-capable buffers, 4 KiB aligned like DAOS's DMA chunks */
		ws[i].wbuf = spdk_zmalloc(g_region, 4096, NULL,
					  SPDK_ENV_LCORE_ID_ANY, SPDK_MALLOC_DMA);
		ws[i].rbuf = spdk_zmalloc(g_region, 4096, NULL,
					  SPDK_ENV_LCORE_ID_ANY, SPDK_MALLOC_DMA);
		if (!ws[i].wbuf || !ws[i].rbuf) {
			fprintf(stderr, "spdk_zmalloc failed at %d\n", i);
			return 2;
		}
	}

	for (int i = 0; i < g_queues; i++)
		pthread_create(&th[i], NULL, worker_fn, &ws[i]);
	for (int i = 0; i < g_queues; i++)
		pthread_join(th[i], NULL);

	int ret = 0;
	for (int i = 0; i < g_queues; i++)
		if (ws[i].rc) {
			fprintf(stderr, "worker %d failed rc=%d\n", i, ws[i].rc);
			ret = 2;
		}

	printf("%s: %ld/%ld reads corrupt (raw SPDK NVMe, no DAOS)\n",
	       g_bad ? "FAIL" : "PASS", g_bad, g_reads);
	if (g_bad && !ret)
		ret = 1;
	return ret;
}
