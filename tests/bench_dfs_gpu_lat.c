/* Per-chunk latency of the GPU-direct and host-staging read paths.
 *
 * Phase B of gpudirect/PLAN.md. The throughput bench (bench_dfs_gpu.c) cannot
 * answer the latency question: its chunk_ms is total wall time divided by chunk
 * count, so with several workers it reports a pipelined average in which one
 * thread's H2D copy hides under another thread's transfer. That is the right
 * number for saturated throughput and the wrong one for TTFT, where what
 * matters is how long ONE chunk takes when nothing else is in flight.
 *
 * Two things are done differently here.
 *
 * 1. Every chunk is timed individually and the distribution is reported, not
 *    just a mean. TTFT is a tail property.
 *
 * 2. For the staging arms the transfer and the copy are timed SEPARATELY
 *    inside the same chunk:
 *
 *        t0 -> dfs_read -> t1 -> cuMemcpyHtoD -> t2
 *        read = t1-t0,  copy = t2-t1
 *
 *    so the serialised copy cost is measured directly rather than inferred by
 *    subtracting two runs. The 'pinned' arm (same read, no copy) is still run
 *    as an independent check: its read distribution should match the
 *    pinnedcopy arm's read distribution. If those two disagree, the split is
 *    not trustworthy and neither is the copy number.
 *
 * cuMemcpyHtoD (not ...Async) blocks until the copy completes, so timing
 * around it is valid. dfs_read/dfs_read_gpu are called with a NULL event and
 * are likewise synchronous.
 *
 * Chunk size is in KiB here, not MiB: LMCache's chunk is a token count, and
 * the resulting KV slice can be well under a megabyte for small models or
 * heavily grouped attention. The copy's fixed overhead matters most exactly
 * there, so the sweep has to be able to reach it.
 *
 *   gcc -O2 -o bench_dfs_gpu_lat bench_dfs_gpu_lat.c -I$P/include \
 *       -L$P/lib64 -ldaos -ldfs -lgurt -lcart -luuid -lcuda -pthread \
 *       -Wl,-rpath,$P/lib64
 *
 *   ./bench_dfs_gpu_lat <pool> <cont> <arm> [chunk_KiB] [total_MiB] [workers]
 *
 * total_MiB is the working set one TTFT would have to fetch, so the reported
 * total_ms IS that path's contribution to TTFT at the given concurrency.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <pthread.h>
#include <time.h>
#include <sys/stat.h>
#include <daos.h>
#include <daos_fs.h>

typedef unsigned long long CUdeviceptr;
typedef int  CUresult, CUdevice;
typedef void *CUcontext;
extern CUresult cuInit(unsigned int);
extern CUresult cuDeviceGet(CUdevice *, int);
extern CUresult cuCtxCreate_v2(CUcontext *, unsigned int, CUdevice);
extern CUresult cuCtxSetCurrent(CUcontext);
extern CUresult cuMemAlloc_v2(CUdeviceptr *, size_t);
extern CUresult cuMemcpyHtoD_v2(CUdeviceptr, const void *, size_t);
extern CUresult cuCtxSynchronize(void);
extern CUresult cuMemHostAlloc(void **, size_t, unsigned int);

#define CU(call) do { CUresult _r = (call); if (_r != 0) {                      \
	fprintf(stderr, "CUDA %s = %d\n", #call, _r); exit(2); } } while (0)
#define DC(call) do { int _r = (call); if (_r != 0) {                           \
	fprintf(stderr, "%s = %d\n", #call, _r); exit(3); } } while (0)

/* ARM_HOSTATTR is the discriminating experiment for the small-transfer gap.
 * At 256 KiB the gpu arm beats every host arm by 41-64%, which the H2D copy
 * cannot explain (it is 2.5% of chunk latency there) and which the host vs
 * pinned controls do not explain either. Two candidates remain: GPU memory
 * itself is faster to fill, or dfs_read_gpu's code path is cheaper than
 * dfs_read's regardless of where the data lands.
 *
 * daos_mem_type_t has DAOS_MEM_TYPE_HOST = 0, so this arm calls dfs_read_gpu
 * with a pinned HOST buffer -- the _gpu entry point, host destination. If it
 * comes out fast, the win is the code path and the ordinary connector can have
 * it; if it stays slow, the win belongs to device memory. */
enum arm { ARM_GPU, ARM_PINNEDCOPY, ARM_HOSTCOPY, ARM_PINNED, ARM_HOST,
	   ARM_HOSTATTR };

static enum arm          arm;
static int               wants_h2d, wants_pinned;
static size_t            chunk, total;
static dfs_t            *dfs;
static CUcontext         cuctx;
static const char       *fname;
static pthread_barrier_t bar;

struct worker {
	pthread_t   th;
	long        id;
	size_t      off, len, moved;
	dfs_obj_t  *obj;
	CUdeviceptr dptr;
	char       *hbuf;
	int         rc;
	/* per-chunk samples, in microseconds */
	double     *lat_read, *lat_copy;
	size_t      nsamp;
};

static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + 1e-9 * t.tv_nsec;
}

static int cmp_d(const void *a, const void *b)
{
	double x = *(const double *)a, y = *(const double *)b;
	return (x > y) - (x < y);
}

/* Percentile of an already-sorted array, nearest-rank. */
static double pct(const double *v, size_t n, double p)
{
	size_t i;

	if (!n)
		return 0.0;
	i = (size_t)(p / 100.0 * (double)(n - 1) + 0.5);
	return v[i];
}

static void ensure_file(const char *name, size_t want)
{
	dfs_obj_t  *obj = NULL;
	d_sg_list_t sgl;
	d_iov_t     iov;
	struct stat st;
	char       *buf;
	size_t      wchunk = 4UL << 20;	/* write in 4 MiB units regardless of
					 * the read chunk, so a small-chunk run
					 * does not spend minutes creating the
					 * file */
	int         rc;

	rc = dfs_stat(dfs, NULL, name, &st);
	if (rc == 0 && (size_t)st.st_size >= want)
		return;

	fprintf(stderr, "creating %s (%zu MiB)...\n", name, want >> 20);
	rc = dfs_open(dfs, NULL, name, S_IFREG | 0644,
		      O_CREAT | O_RDWR | O_TRUNC, 0, 0, NULL, &obj);
	if (rc) { fprintf(stderr, "dfs_open(create) = %d\n", rc); exit(3); }

	if (posix_memalign((void **)&buf, 4096, wchunk)) { perror("memalign"); exit(2); }
	for (size_t i = 0; i < wchunk; i++)
		buf[i] = (char)(i * 31u);

	sgl.sg_nr = 1; sgl.sg_iovs = &iov;
	for (size_t off = 0; off < want; off += wchunk) {
		sgl.sg_nr_out = 0;
		d_iov_set(&iov, buf, wchunk);
		rc = dfs_write(dfs, obj, &sgl, off, NULL);
		if (rc) { fprintf(stderr, "dfs_write = %d\n", rc); exit(3); }
	}
	free(buf);
	dfs_release(obj);
}

/* Read one chunk, recording the transfer and the copy separately. */
static int read_one(struct worker *w, size_t off, int timed)
{
	d_sg_list_t sgl;
	d_iov_t     iov;
	daos_size_t got = 0;
	double      t0, t1, t2;
	int         rc;

	sgl.sg_nr = 1; sgl.sg_nr_out = 0; sgl.sg_iovs = &iov;
	t0 = now();
	if (arm == ARM_GPU || arm == ARM_HOSTATTR) {
		daos_mem_attr_t ma;

		memset(&ma, 0, sizeof(ma));
		if (arm == ARM_GPU) {
			ma.ma_mem_type  = DAOS_MEM_TYPE_CUDA;
			ma.ma_device_id = 0;
			d_iov_set(&iov, (void *)(uintptr_t)w->dptr, chunk);
		} else {
			ma.ma_mem_type  = DAOS_MEM_TYPE_HOST;
			ma.ma_device_id = 0;
			d_iov_set(&iov, w->hbuf, chunk);
		}
		rc = dfs_read_gpu(dfs, w->obj, &sgl, off, &got, &ma);
		t1 = t2 = now();
	} else {
		d_iov_set(&iov, w->hbuf, chunk);
		rc = dfs_read(dfs, w->obj, &sgl, off, &got, NULL);
		t1 = now();
		if (rc == 0 && wants_h2d)
			CU(cuMemcpyHtoD_v2(w->dptr, w->hbuf, chunk));
		t2 = now();
	}
	if (rc)
		return rc;
	if (got != chunk) {
		fprintf(stderr, "short read at %zu: %zu of %zu\n",
			off, (size_t)got, chunk);
		return -1;
	}
	if (timed) {
		w->lat_read[w->nsamp] = (t1 - t0) * 1e6;
		w->lat_copy[w->nsamp] = (t2 - t1) * 1e6;
		w->nsamp++;
	}
	w->moved += got;
	return 0;
}

static void *worker_main(void *a)
{
	struct worker *w = a;

	CU(cuCtxSetCurrent(cuctx));

	w->rc = dfs_open(dfs, NULL, fname, S_IFREG | 0644, O_RDWR, 0, 0, NULL,
			 &w->obj);
	if (w->rc)
		goto ready;

	if (arm == ARM_GPU || wants_h2d)
		CU(cuMemAlloc_v2(&w->dptr, chunk));
	if (arm != ARM_GPU) {
		if (wants_pinned) {
			CU(cuMemHostAlloc((void **)&w->hbuf, chunk, 0));
		} else if (posix_memalign((void **)&w->hbuf, 4096, chunk)) {
			perror("memalign");
			w->rc = -1;
			goto ready;
		}
		memset(w->hbuf, 0, chunk);
	}

	/* Untimed chunk: connection setup and first-touch registration must not
	 * land in the distribution, and at these chunk sizes one outlier moves
	 * p99 a long way. */
	w->rc = read_one(w, w->off, 0);
	w->moved = 0;

ready:
	pthread_barrier_wait(&bar);
	if (w->rc)
		return NULL;

	for (size_t o = w->off; o < w->off + w->len; o += chunk) {
		w->rc = read_one(w, o, 1);
		if (w->rc)
			return NULL;
	}
	if (arm == ARM_GPU || wants_h2d)
		CU(cuCtxSynchronize());
	return NULL;
}

int main(int argc, char **argv)
{
	const char *pool  = argc > 1 ? argv[1] : "gdspool";
	const char *cont  = argc > 2 ? argv[2] : "kvgds";
	const char *armnm = argc > 3 ? argv[3] : "gpu";
	int         nw    = argc > 6 ? atoi(argv[6]) : 1;
	daos_handle_t poh, coh;
	CUdevice    dev;
	struct worker *w;
	double     *all_read, *all_copy, *all_tot;
	size_t      nall = 0, chunks_per, slice, moved = 0;
	double      t0, dt;
	char        name[128];
	int         fails = 0;

	chunk = (size_t)(argc > 4 ? atol(argv[4]) : 4096) << 10;	/* KiB */
	total = (size_t)(argc > 5 ? atol(argv[5]) : 1024) << 20;	/* MiB */

	if      (!strcmp(armnm, "gpu"))        arm = ARM_GPU;
	else if (!strcmp(armnm, "pinnedcopy")) arm = ARM_PINNEDCOPY;
	else if (!strcmp(armnm, "hostcopy"))   arm = ARM_HOSTCOPY;
	else if (!strcmp(armnm, "pinned"))     arm = ARM_PINNED;
	else if (!strcmp(armnm, "host"))       arm = ARM_HOST;
	else if (!strcmp(armnm, "hostattr"))   arm = ARM_HOSTATTR;
	else {
		fprintf(stderr,
			"arm: gpu|pinnedcopy|hostcopy|pinned|host|hostattr\n");
		return 1;
	}
	wants_h2d = (arm == ARM_PINNEDCOPY || arm == ARM_HOSTCOPY);
	/* hostattr must be pinned like the 'pinned' arm: the two differ only by
	 * which entry point is called, or the comparison proves nothing. */
	wants_pinned = (arm == ARM_PINNEDCOPY || arm == ARM_PINNED ||
			arm == ARM_HOSTATTR);

	if (nw < 1 || nw > 256) { fprintf(stderr, "workers out of range\n"); return 1; }
	total -= total % chunk;
	chunks_per = (total / chunk) / (size_t)nw;
	if (chunks_per < 1) {
		fprintf(stderr, "%d workers need at least %d chunks\n", nw, nw);
		return 1;
	}
	slice = chunks_per * chunk;

	CU(cuInit(0));
	CU(cuDeviceGet(&dev, 0));
	CU(cuCtxCreate_v2(&cuctx, 0, dev));

	DC(daos_init());
	DC(daos_pool_connect(pool, NULL, DAOS_PC_RW, &poh, NULL, NULL));
	DC(daos_cont_open(poh, cont, DAOS_COO_RW, &coh, NULL, NULL));
	DC(dfs_mount(poh, coh, O_RDWR, &dfs));

	/* File is named by total only, so every arm and chunk size at a given
	 * working set reads identical bytes. */
	snprintf(name, sizeof(name), "lat_%zuMiB", total >> 20);
	ensure_file(name, total);
	fname = name;

	w = calloc((size_t)nw, sizeof(*w));
	for (int i = 0; i < nw; i++) {
		w[i].id  = i;
		w[i].off = (size_t)i * slice;
		w[i].len = slice;
		w[i].lat_read = calloc(chunks_per, sizeof(double));
		w[i].lat_copy = calloc(chunks_per, sizeof(double));
	}
	pthread_barrier_init(&bar, NULL, (unsigned)nw + 1);

	for (int i = 0; i < nw; i++)
		pthread_create(&w[i].th, NULL, worker_main, &w[i]);
	pthread_barrier_wait(&bar);
	t0 = now();
	for (int i = 0; i < nw; i++)
		pthread_join(w[i].th, NULL);
	dt = now() - t0;

	for (int i = 0; i < nw; i++) {
		if (w[i].rc) { fails++; continue; }
		moved += w[i].moved;
		nall  += w[i].nsamp;
	}
	if (fails) {
		fprintf(stderr, "%d of %d workers failed\n", fails, nw);
		return 4;
	}

	all_read = calloc(nall, sizeof(double));
	all_copy = calloc(nall, sizeof(double));
	all_tot  = calloc(nall, sizeof(double));
	for (int i = 0, k = 0; i < nw; i++)
		for (size_t j = 0; j < w[i].nsamp; j++, k++) {
			all_read[k] = w[i].lat_read[j];
			all_copy[k] = w[i].lat_copy[j];
			all_tot[k]  = w[i].lat_read[j] + w[i].lat_copy[j];
		}
	qsort(all_read, nall, sizeof(double), cmp_d);
	qsort(all_copy, nall, sizeof(double), cmp_d);
	qsort(all_tot,  nall, sizeof(double), cmp_d);

	double mean = 0.0;
	for (size_t i = 0; i < nall; i++)
		mean += all_tot[i];
	mean /= (double)nall;

	/* One line, all microseconds except total_ms and GB/s. read_* and
	 * copy_* are the split; tot_* is what a caller waiting on one chunk
	 * actually sees. total_ms is the whole working set, i.e. this path's
	 * contribution to TTFT at this concurrency. */
	printf("arm=%-10s workers=%-3d chunk_KiB=%-6zu ws_MiB=%-5zu "
	       "GB/s=%.2f total_ms=%.3f n=%zu "
	       "tot_p50=%.1f tot_p90=%.1f tot_p95=%.1f tot_p99=%.1f "
	       "tot_max=%.1f tot_mean=%.1f "
	       "read_p50=%.1f read_p99=%.1f copy_p50=%.1f copy_p99=%.1f\n",
	       armnm, nw, chunk >> 10, moved >> 20,
	       moved / dt / 1e9, dt * 1e3, nall,
	       pct(all_tot, nall, 50), pct(all_tot, nall, 90),
	       pct(all_tot, nall, 95), pct(all_tot, nall, 99),
	       all_tot[nall - 1], mean,
	       pct(all_read, nall, 50), pct(all_read, nall, 99),
	       pct(all_copy, nall, 50), pct(all_copy, nall, 99));

	dfs_umount(dfs);
	daos_cont_close(coh, NULL);
	daos_pool_disconnect(poh, NULL);
	daos_fini();
	return 0;
}
