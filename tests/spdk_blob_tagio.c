/*
 * spdk_blob_tagio.c -- DAOS-free reproducer at the SPDK *blobstore* layer.
 *
 * Where this sits in the hunt (see DAOS-CONCURRENT-READ-CORRUPTION.md):
 *   §27  class:file (blobstore over aio bdev)      -> clean
 *        class:nvme (blobstore over nvme bdev)     -> up to 24% corrupt
 *   §28  raw SPDK NVMe driver, no blobstore        -> clean (0/2560)
 * So the fault lives in the blobstore-over-nvme-bdev combination, or in how
 * DAOS drives it. This program removes DAOS but keeps the blobstore: several
 * blobs, 4 MiB tagged I/O through spdk_blob_io_write/read, many channels.
 *
 *   corrupt here -> the defect is SPDK's (blobstore/bdev_nvme), and the report
 *                   belongs to the SPDK project with this as the reproducer
 *   clean here   -> SPDK is exonerated at every layer and the defect is in
 *                   DAOS's bio blob usage (cluster/offset arithmetic, channel
 *                   handling, or buffer lifetime around spdk_blob_io_read)
 *
 * Shape chosen to mirror DAOS: one blob per worker (like one VOS blob per
 * target), 4 MiB per I/O (one DFS chunk), each worker on its own io_channel,
 * all workers hammering concurrently, and the same self-describing payload
 * (region<<48)|(round<<40)|offset so a wrong region names its own origin.
 *
 * Runs on SPDK's app framework (single reactor, asynchronous state machine),
 * because blobstore calls must be issued from an SPDK thread.
 *
 * DESTRUCTIVE: spdk_bs_init() wipes the device. Point it only at a drive whose
 * contents you intend to destroy.
 *
 *   ./spdk_blob_tagio -b <bdev_name> [-w workers] [-r rounds] [-s MiB]
 *   (bdev is created from JSON config passed via -c, e.g. an nvme bdev)
 *
 * Build: see tests/spdk_blob_tagio.build.sh
 */
#include "spdk/stdinc.h"
#include "spdk/bdev.h"
#include "spdk/blob.h"
#include "spdk/blob_bdev.h"
#include "spdk/env.h"
#include "spdk/event.h"
#include "spdk/log.h"
#include "spdk/string.h"
#include "spdk/thread.h"

#define MAX_WORKERS 32

static char    *g_bdev_name = "Nvme0n1";
static int      g_workers   = 8;
static int      g_rounds    = 20;
static uint64_t g_io_size   = 4ul << 20;   /* one DAOS DFS chunk */

struct worker {
	int                      id;
	struct spdk_blob        *blob;
	spdk_blob_id             blobid;
	struct spdk_io_channel  *ch;
	uint8_t                 *wbuf;
	uint8_t                 *rbuf;
	int                      round;
	uint64_t                 io_pages;    /* io size in blobstore pages */
	bool                     done;
};

struct ctx {
	struct spdk_blob_store *bs;
	uint64_t                page_size;
	struct worker           w[MAX_WORKERS];
	int                     pending;
	long                    reads;
	long                    bad;
	int                     rc;
};

static struct ctx g_ctx;

/* ---- tagged payload ----------------------------------------------------- */
static inline uint64_t tag_of(int region, int round, uint64_t off)
{
	return ((uint64_t)(region & 0xffff) << 48) |
	       ((uint64_t)(round & 0xff) << 40) |
	       (off & 0x000000ffffffffffULL);
}

static void fill_tagged(void *buf, int region, int round, uint64_t n)
{
	uint64_t *w = buf;

	for (uint64_t i = 0; i < n / 8; i++)
		w[i] = tag_of(region, round, i * 8);
}

static void diagnose(const void *got, uint64_t n, int region, int round,
		     char *out, size_t outsz)
{
	const uint64_t *w = got;
	uint64_t n_ok = 0, n_zero = 0, n_reg = 0, n_round = 0, n_off = 0, n_junk = 0;
	uint64_t first = UINT64_MAX, fv = 0;

	for (uint64_t i = 0; i < n / 8; i++) {
		if (w[i] == tag_of(region, round, i * 8)) { n_ok++; continue; }
		if (first == UINT64_MAX) { first = i * 8; fv = w[i]; }
		if (w[i] == 0) { n_zero++; continue; }

		int      r  = (int)(w[i] >> 48);
		int      rd = (int)((w[i] >> 40) & 0xff);
		uint64_t o  = w[i] & 0x000000ffffffffffULL;

		if (r != region && r < g_workers)    n_reg++;
		else if (r == region && rd != round) n_round++;
		else if (r == region && o != i * 8)  n_off++;
		else                                 n_junk++;
	}
	snprintf(out, outsz,
		 "first bad at %lu: region=%d round=%d off=%lu | ok=%lu zero=%lu "
		 "foreign-blob=%lu stale-round=%lu shifted=%lu junk=%lu",
		 (unsigned long)(first == UINT64_MAX ? 0 : first),
		 (int)(fv >> 48), (int)((fv >> 40) & 0xff),
		 (unsigned long)(fv & 0x000000ffffffffffULL),
		 (unsigned long)n_ok, (unsigned long)n_zero, (unsigned long)n_reg,
		 (unsigned long)n_round, (unsigned long)n_off, (unsigned long)n_junk);
}

/* ---- state machine ------------------------------------------------------ */
static void unload_complete(void *arg, int bserrno)
{
	(void)arg;
	if (bserrno)
		SPDK_ERRLOG("bs unload failed: %s\n", spdk_strerror(-bserrno));
	spdk_app_stop(g_ctx.bad ? 1 : g_ctx.rc);
}

/*
 * Close blobs before unloading: spdk_bs_unload() refuses while any blob is
 * open. The verdict is printed here rather than in unload_complete so a
 * teardown hiccup can never hide the measurement.
 */
static void close_next(void *arg, int bserrno)
{
	static int idx;

	(void)arg;
	if (bserrno)
		SPDK_ERRLOG("blob close failed: %s\n", spdk_strerror(-bserrno));

	while (idx < g_workers) {
		struct worker *w = &g_ctx.w[idx++];

		if (w->ch) {
			spdk_bs_free_io_channel(w->ch);
			w->ch = NULL;
		}
		spdk_free(w->wbuf); w->wbuf = NULL;
		spdk_free(w->rbuf); w->rbuf = NULL;
		if (w->blob) {
			struct spdk_blob *b = w->blob;

			w->blob = NULL;
			spdk_blob_close(b, close_next, NULL);
			return;
		}
	}
	spdk_bs_unload(g_ctx.bs, unload_complete, NULL);
}

static void teardown(void)
{
	printf("%s: %ld/%ld reads corrupt (SPDK blobstore, no DAOS)\n",
	       g_ctx.bad ? "FAIL" : "PASS", g_ctx.bad, g_ctx.reads);
	fflush(stdout);
	close_next(NULL, 0);
}

static void worker_step(struct worker *w);

static void read_done(void *arg, int bserrno)
{
	struct worker *w = arg;
	char detail[256];

	if (bserrno) {
		SPDK_ERRLOG("blob %d read failed: %s\n", w->id, spdk_strerror(-bserrno));
		g_ctx.rc = 2;
		w->done = true;
		if (--g_ctx.pending == 0)
			teardown();
		return;
	}

	g_ctx.reads++;
	if (memcmp(w->rbuf, w->wbuf, g_io_size) != 0) {
		g_ctx.bad++;
		diagnose(w->rbuf, g_io_size, w->id, w->round, detail, sizeof(detail));
		printf("  CORRUPT blob%-2d r%-2d %s\n", w->id, w->round, detail);
		fflush(stdout);
	}

	w->round++;
	worker_step(w);
}

static void write_done(void *arg, int bserrno)
{
	struct worker *w = arg;

	if (bserrno) {
		SPDK_ERRLOG("blob %d write failed: %s\n", w->id, spdk_strerror(-bserrno));
		g_ctx.rc = 2;
		w->done = true;
		if (--g_ctx.pending == 0)
			teardown();
		return;
	}
	/* Poison the destination so a no-op read cannot pass. */
	memset(w->rbuf, 0xA5, g_io_size);
	spdk_blob_io_read(w->blob, w->ch, w->rbuf, 0, w->io_pages, read_done, w);
}

static void worker_step(struct worker *w)
{
	if (w->round >= g_rounds) {
		w->done = true;
		if (--g_ctx.pending == 0)
			teardown();
		return;
	}
	fill_tagged(w->wbuf, w->id, w->round, g_io_size);
	spdk_blob_io_write(w->blob, w->ch, w->wbuf, 0, w->io_pages, write_done, w);
}

static void blob_open_complete(void *arg, struct spdk_blob *blob, int bserrno)
{
	struct worker *w = arg;

	if (bserrno) {
		SPDK_ERRLOG("open blob %d: %s\n", w->id, spdk_strerror(-bserrno));
		spdk_app_stop(-1);
		return;
	}
	w->blob = blob;
	w->ch = spdk_bs_alloc_io_channel(g_ctx.bs);
	w->wbuf = spdk_zmalloc(g_io_size, 0x1000, NULL, SPDK_ENV_LCORE_ID_ANY,
			       SPDK_MALLOC_DMA);
	w->rbuf = spdk_zmalloc(g_io_size, 0x1000, NULL, SPDK_ENV_LCORE_ID_ANY,
			       SPDK_MALLOC_DMA);
	if (!w->ch || !w->wbuf || !w->rbuf) {
		SPDK_ERRLOG("alloc failed for worker %d\n", w->id);
		spdk_app_stop(-1);
		return;
	}
	w->io_pages = g_io_size / g_ctx.page_size;

	if (--g_ctx.pending == 0) {
		/* All blobs open: release every worker at once. */
		g_ctx.pending = g_workers;
		for (int i = 0; i < g_workers; i++)
			worker_step(&g_ctx.w[i]);
	}
}

static void blob_resize_complete(void *arg, int bserrno)
{
	struct worker *w = arg;

	if (bserrno) {
		SPDK_ERRLOG("resize blob %d: %s\n", w->id, spdk_strerror(-bserrno));
		spdk_app_stop(-1);
		return;
	}
	spdk_blob_sync_md(w->blob, (spdk_blob_op_complete)blob_open_complete, w);
}

static void blob_create_complete(void *arg, spdk_blob_id blobid, int bserrno)
{
	struct worker *w = arg;

	if (bserrno) {
		SPDK_ERRLOG("create blob %d: %s\n", w->id, spdk_strerror(-bserrno));
		spdk_app_stop(-1);
		return;
	}
	w->blobid = blobid;
	spdk_bs_open_blob(g_ctx.bs, blobid, blob_open_complete, w);
}

static void bs_init_complete(void *arg, struct spdk_blob_store *bs, int bserrno)
{
	(void)arg;
	uint64_t clusters_needed;

	if (bserrno) {
		SPDK_ERRLOG("bs init: %s\n", spdk_strerror(-bserrno));
		spdk_app_stop(-1);
		return;
	}
	g_ctx.bs = bs;
	g_ctx.page_size = spdk_bs_get_page_size(bs);
	clusters_needed = g_io_size / spdk_bs_get_cluster_size(bs) + 1;

	printf("blobstore: page=%lu cluster=%lu free_clusters=%lu | "
	       "workers=%d rounds=%d io=%luMiB\n",
	       (unsigned long)g_ctx.page_size,
	       (unsigned long)spdk_bs_get_cluster_size(bs),
	       (unsigned long)spdk_bs_free_cluster_count(bs),
	       g_workers, g_rounds, (unsigned long)(g_io_size >> 20));

	/*
	 * Create the blobs with an explicit size so each one owns enough
	 * clusters for a full 4 MiB I/O -- the point is to exercise the
	 * cluster mapping that class:nvme and class:file disagree on.
	 */
	g_ctx.pending = g_workers;
	for (int i = 0; i < g_workers; i++) {
		struct spdk_blob_opts opts;

		g_ctx.w[i].id = i;
		spdk_blob_opts_init(&opts, sizeof(opts));
		opts.num_clusters = clusters_needed;
		spdk_bs_create_blob_ext(bs, &opts, blob_create_complete, &g_ctx.w[i]);
	}
}

static void base_bdev_event_cb(enum spdk_bdev_event_type type,
			       struct spdk_bdev *bdev, void *event_ctx)
{
	(void)type; (void)bdev; (void)event_ctx;
}

static void app_start(void *arg)
{
	struct spdk_bs_dev *bs_dev = NULL;
	int rc;

	(void)arg;
	rc = spdk_bdev_create_bs_dev_ext(g_bdev_name, base_bdev_event_cb, NULL,
					 &bs_dev);
	if (rc) {
		SPDK_ERRLOG("could not open bdev %s: %s\n", g_bdev_name,
			    spdk_strerror(-rc));
		spdk_app_stop(-1);
		return;
	}
	spdk_bs_init(bs_dev, NULL, bs_init_complete, NULL);
}

static void usage(void)
{
	printf(" -b <bdev>   bdev name to build the blobstore on (default Nvme0n1)\n");
	printf(" -w <n>      workers/blobs (default 8)\n");
	printf(" -N <n>      rounds per worker (default 20)\n");
	printf(" -S <MiB>    I/O size (default 4)\n");
}

static int parse_arg(int ch, char *arg)
{
	switch (ch) {
	case 'b': g_bdev_name = arg; break;
	case 'w': g_workers = spdk_strtol(arg, 10); break;
	case 'N': g_rounds = spdk_strtol(arg, 10); break;
	case 'S': g_io_size = (uint64_t)spdk_strtol(arg, 10) << 20; break;
	default: return -EINVAL;
	}
	return 0;
}

int main(int argc, char **argv)
{
	struct spdk_app_opts opts = {};
	int rc;

	spdk_app_opts_init(&opts, sizeof(opts));
	opts.name = "spdk_blob_tagio";
	rc = spdk_app_parse_args(argc, argv, &opts, "b:w:N:S:", NULL,
				 parse_arg, usage);
	if (rc != SPDK_APP_PARSE_ARGS_SUCCESS)
		return rc;
	if (g_workers > MAX_WORKERS)
		g_workers = MAX_WORKERS;

	rc = spdk_app_start(&opts, app_start, NULL);
	spdk_app_fini();
	return rc;
}
