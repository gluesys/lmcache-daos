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
 * §41.4 additions, after the DAOS side was measured clean at every layer:
 *   -Y MiB   blob capacity, so a blob spans MANY clusters and each round writes
 *            at a DIFFERENT non-zero offset.  The original version sized blobs
 *            to a single I/O and always wrote at offset 0, which never
 *            exercises the cluster-map offset translation that the observed
 *            adjacent-chunk slide (§40.2) points at.
 *   -K n     WAL-like blobs taking continuous SMALL writes at rotating offsets
 *            while the 4 MiB traffic runs.  MD-on-SSD drives WAL, meta and data
 *            blobs concurrently on one blobstore; uniform 4 MiB I/O does not.
 *   -X       share a single io_channel across all blobs, the way DAOS shares
 *            one channel per xstream, instead of one channel per blob.
 * The payload tag carries the blob byte offset, so data that slid in from
 * another offset of the same blob is named as precisely as a foreign blob.
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
#define MAX_WAL     8
#define MAX_BLOBS   (MAX_WORKERS + MAX_WAL)

static char    *g_bdev_name = "Nvme0n1";
static int      g_workers   = 8;
static int      g_rounds    = 20;
static uint64_t g_io_size   = 4ul << 20;   /* one DAOS DFS chunk */
static uint64_t g_cap       = 128ul << 20; /* blob capacity: many clusters */
static int      g_wal       = 1;           /* WAL-like small-write blobs */
static uint64_t g_small     = 8ul << 10;   /* their I/O size */
static int      g_share_ch;                /* one channel for every blob */

struct worker {
	int                      id;
	struct spdk_blob        *blob;
	spdk_blob_id             blobid;
	struct spdk_io_channel  *ch;
	uint8_t                 *wbuf;
	uint8_t                 *rbuf;
	int                      round;
	uint64_t                 io_pages;    /* io size in blobstore pages */
	uint64_t                 cap_pages;   /* blob capacity in pages */
	uint64_t                 off_pages;   /* where this round writes */
	bool                     is_wal;
	long                     ios;
	bool                     done;
};

struct ctx {
	struct spdk_blob_store *bs;
	uint64_t                page_size;
	struct worker           w[MAX_BLOBS];
	struct spdk_io_channel *shared_ch;
	int                     nblobs;
	int                     pending;
	int                     wal_active;
	bool                    data_done;
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

/* base is the blob byte offset this buffer is going to, so a slide from
 * another offset of the same blob is as identifiable as a foreign blob. */
static void fill_tagged(void *buf, int region, int round, uint64_t base,
			uint64_t n)
{
	uint64_t *w = buf;

	for (uint64_t i = 0; i < n / 8; i++)
		w[i] = tag_of(region, round, base + i * 8);
}

static void diagnose(const void *got, uint64_t n, int region, int round,
		     uint64_t base, char *out, size_t outsz)
{
	const uint64_t *w = got;
	uint64_t n_ok = 0, n_zero = 0, n_reg = 0, n_round = 0, n_off = 0, n_junk = 0;
	uint64_t first = UINT64_MAX, fv = 0;

	for (uint64_t i = 0; i < n / 8; i++) {
		if (w[i] == tag_of(region, round, base + i * 8)) { n_ok++; continue; }
		if (first == UINT64_MAX) { first = i * 8; fv = w[i]; }
		if (w[i] == 0) { n_zero++; continue; }

		int      r  = (int)(w[i] >> 48);
		int      rd = (int)((w[i] >> 40) & 0xff);
		uint64_t o  = w[i] & 0x000000ffffffffffULL;

		if (r != region && r < g_ctx.nblobs)        n_reg++;
		else if (r == region && rd != round)        n_round++;
		else if (r == region && o != base + i * 8)  n_off++;
		else                                        n_junk++;
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

	while (idx < g_ctx.nblobs) {
		struct worker *w = &g_ctx.w[idx++];

		if (w->ch && w->ch != g_ctx.shared_ch) {
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
	if (g_ctx.shared_ch) {
		spdk_bs_free_io_channel(g_ctx.shared_ch);
		g_ctx.shared_ch = NULL;
	}
	spdk_bs_unload(g_ctx.bs, unload_complete, NULL);
}

static void teardown(void)
{
	long wal_ios = 0;

	for (int i = g_workers; i < g_ctx.nblobs; i++)
		wal_ios += g_ctx.w[i].ios;
	printf("%s: %ld/%ld reads corrupt (SPDK blobstore, no DAOS) | "
	       "wal-blobs=%d small ios=%ld\n",
	       g_ctx.bad ? "FAIL" : "PASS", g_ctx.bad, g_ctx.reads,
	       g_ctx.nblobs - g_workers, wal_ios);
	fflush(stdout);
	close_next(NULL, 0);
}

/*
 * The data workers finished; let the WAL blobs drain their in-flight write and
 * stop.  Teardown waits for them, otherwise spdk_bs_unload() would race a
 * live I/O.
 */
static void maybe_teardown(void)
{
	if (g_ctx.pending == 0 && g_ctx.wal_active == 0)
		teardown();
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
		g_ctx.pending--;
		if (g_ctx.pending == 0)
			g_ctx.data_done = true;
		maybe_teardown();
		return;
	}

	g_ctx.reads++;
	if (memcmp(w->rbuf, w->wbuf, g_io_size) != 0) {
		uint64_t base = w->off_pages * g_ctx.page_size;

		g_ctx.bad++;
		diagnose(w->rbuf, g_io_size, w->id, w->round, base, detail,
			 sizeof(detail));
		printf("  CORRUPT blob%-2d r%-2d at blob offset %luMiB | %s\n",
		       w->id, w->round, (unsigned long)(base >> 20), detail);
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
		g_ctx.pending--;
		if (g_ctx.pending == 0)
			g_ctx.data_done = true;
		maybe_teardown();
		return;
	}
	/* Poison the destination so a no-op read cannot pass. */
	memset(w->rbuf, 0xA5, g_io_size);
	spdk_blob_io_read(w->blob, w->ch, w->rbuf, w->off_pages, w->io_pages,
			  read_done, w);
}

/*
 * WAL-like traffic: small writes, rotating offsets, issued back to back for as
 * long as the 4 MiB workers are running.  This is the condition §28 and §29
 * lacked -- MD-on-SSD keeps WAL and data blobs busy on the same blobstore.
 */
static void wal_step(struct worker *w);

static void wal_write_done(void *arg, int bserrno)
{
	struct worker *w = arg;

	if (bserrno) {
		SPDK_ERRLOG("wal blob %d write failed: %s\n", w->id,
			    spdk_strerror(-bserrno));
		g_ctx.rc = 2;
		g_ctx.wal_active--;
		maybe_teardown();
		return;
	}
	w->ios++;
	wal_step(w);
}

static void wal_step(struct worker *w)
{
	uint64_t slots, slot;

	if (g_ctx.data_done) {
		w->done = true;
		g_ctx.wal_active--;
		maybe_teardown();
		return;
	}
	slots = w->cap_pages / w->io_pages;
	slot = (uint64_t)(w->ios * 3 + w->id) % slots;
	w->off_pages = slot * w->io_pages;
	fill_tagged(w->wbuf, w->id, (int)(w->ios & 0xff),
		    w->off_pages * g_ctx.page_size, g_small);
	spdk_blob_io_write(w->blob, w->ch, w->wbuf, w->off_pages, w->io_pages,
			   wal_write_done, w);
}

static void worker_step(struct worker *w)
{
	uint64_t slots, slot;

	if (w->round >= g_rounds) {
		w->done = true;
		g_ctx.pending--;
		if (g_ctx.pending == 0)
			g_ctx.data_done = true;
		maybe_teardown();
		return;
	}
	/*
	 * Rotate the destination so consecutive rounds land on different
	 * clusters, and different workers are writing different offsets at the
	 * same time.  Writing offset 0 every round -- what this program did
	 * before -- never touches the offset translation.
	 */
	slots = w->cap_pages / w->io_pages;
	slot = (uint64_t)(w->round * 3 + w->id) % slots;
	w->off_pages = slot * w->io_pages;
	fill_tagged(w->wbuf, w->id, w->round, w->off_pages * g_ctx.page_size,
		    g_io_size);
	spdk_blob_io_write(w->blob, w->ch, w->wbuf, w->off_pages, w->io_pages,
			   write_done, w);
}

static void blob_open_complete(void *arg, struct spdk_blob *blob, int bserrno)
{
	struct worker *w = arg;

	if (bserrno) {
		SPDK_ERRLOG("open blob %d: %s\n", w->id, spdk_strerror(-bserrno));
		spdk_app_stop(-1);
		return;
	}
	uint64_t sz = w->is_wal ? g_small : g_io_size;

	w->blob = blob;
	if (g_share_ch) {
		if (g_ctx.shared_ch == NULL)
			g_ctx.shared_ch = spdk_bs_alloc_io_channel(g_ctx.bs);
		w->ch = g_ctx.shared_ch;
	} else {
		w->ch = spdk_bs_alloc_io_channel(g_ctx.bs);
	}
	w->wbuf = spdk_zmalloc(sz, 0x1000, NULL, SPDK_ENV_LCORE_ID_ANY,
			       SPDK_MALLOC_DMA);
	w->rbuf = spdk_zmalloc(sz, 0x1000, NULL, SPDK_ENV_LCORE_ID_ANY,
			       SPDK_MALLOC_DMA);
	if (!w->ch || !w->wbuf || !w->rbuf) {
		SPDK_ERRLOG("alloc failed for worker %d\n", w->id);
		spdk_app_stop(-1);
		return;
	}
	w->io_pages = sz / g_ctx.page_size;
	w->cap_pages = g_cap / g_ctx.page_size;

	if (--g_ctx.pending == 0) {
		/* All blobs open: release the WAL traffic, then the workers. */
		g_ctx.pending = g_workers;
		g_ctx.wal_active = g_ctx.nblobs - g_workers;
		for (int i = g_workers; i < g_ctx.nblobs; i++)
			wal_step(&g_ctx.w[i]);
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
	/* capacity, not one I/O: the blob must span many clusters */
	clusters_needed = g_cap / spdk_bs_get_cluster_size(bs) + 1;
	g_ctx.nblobs = g_workers + g_wal;

	printf("blobstore: page=%lu cluster=%lu free_clusters=%lu | "
	       "workers=%d rounds=%d io=%luMiB cap=%luMiB wal_blobs=%d "
	       "wal_io=%luKiB channel=%s\n",
	       (unsigned long)g_ctx.page_size,
	       (unsigned long)spdk_bs_get_cluster_size(bs),
	       (unsigned long)spdk_bs_free_cluster_count(bs),
	       g_workers, g_rounds, (unsigned long)(g_io_size >> 20),
	       (unsigned long)(g_cap >> 20), g_wal,
	       (unsigned long)(g_small >> 10),
	       g_share_ch ? "shared" : "per-blob");

	/*
	 * Every blob gets the full capacity so writes rotate across clusters.
	 * Blobs [0, g_workers) take 4 MiB tagged I/O and verify; the rest are
	 * WAL-like and take continuous small writes on the same blobstore.
	 */
	g_ctx.pending = g_ctx.nblobs;
	for (int i = 0; i < g_ctx.nblobs; i++) {
		struct spdk_blob_opts opts;

		g_ctx.w[i].id = i;
		g_ctx.w[i].is_wal = (i >= g_workers);
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
	printf(" -w <n>      workers/blobs taking 4 MiB tagged I/O (default 8)\n");
	printf(" -N <n>      rounds per worker (default 20)\n");
	printf(" -S <MiB>    I/O size (default 4)\n");
	printf(" -Y <MiB>    blob capacity, so writes rotate over clusters (default 128)\n");
	printf(" -K <n>      WAL-like blobs taking continuous small writes (default 1)\n");
	printf(" -k <KiB>    size of those small writes (default 8)\n");
	printf(" -X          share one io_channel across all blobs\n");
}

static int parse_arg(int ch, char *arg)
{
	switch (ch) {
	case 'b': g_bdev_name = arg; break;
	case 'w': g_workers = spdk_strtol(arg, 10); break;
	case 'N': g_rounds = spdk_strtol(arg, 10); break;
	case 'S': g_io_size = (uint64_t)spdk_strtol(arg, 10) << 20; break;
	case 'Y': g_cap = (uint64_t)spdk_strtol(arg, 10) << 20; break;
	case 'K': g_wal = spdk_strtol(arg, 10); break;
	case 'k': g_small = (uint64_t)spdk_strtol(arg, 10) << 10; break;
	case 'X': g_share_ch = 1; break;
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
	rc = spdk_app_parse_args(argc, argv, &opts, "b:w:N:S:Y:K:k:X", NULL,
				 parse_arg, usage);
	if (rc != SPDK_APP_PARSE_ARGS_SUCCESS)
		return rc;
	if (g_workers > MAX_WORKERS)
		g_workers = MAX_WORKERS;
	if (g_wal > MAX_WAL)
		g_wal = MAX_WAL;
	if (g_cap < g_io_size)
		g_cap = g_io_size;

	rc = spdk_app_start(&opts, app_start, NULL);
	spdk_app_fini();
	return rc;
}
