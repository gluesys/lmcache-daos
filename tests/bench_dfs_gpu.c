/* GPU-direct vs host-staging read path on DAOS DFS.
 *
 * The point of GPU-direct here is not peak bandwidth -- pinned host streaming
 * already sits close to the DAOS ceiling. The point is what it costs the host:
 * CPU cycles per byte, DRAM traffic, and latency. So this program runs exactly
 * ONE arm per invocation and prints one machine-readable line, which lets an
 * outer `perf stat` attribute counters to that arm and nothing else. Running
 * both arms in one process would fold their costs together and make the
 * cycles/byte number meaningless.
 *
 * Arms:
 *   gpu         dfs_read_gpu() straight into device memory
 *   pinnedcopy  dfs_read() into cuMemHostAlloc'd (pinned) host memory, then
 *               cuMemcpyHtoD -- the honest competitor, and what a tuned
 *               CPU-return connector would do
 *   hostcopy    same but with ordinary malloc'd memory, which makes CUDA stage
 *               the copy through its own pinned bounce buffer. Keep it only to
 *               show what the untuned path costs; do not quote it as "staging"
 *   pinned      pinned read with no H2D, to separate storage from the copy
 *   host        ordinary read with no H2D
 *
 * With more than one worker the file is split into contiguous slices, one per
 * thread, and each thread keeps its own buffer and its own open handle. Buffers
 * are allocated once and reused, so what the concurrency sweep exercises is the
 * transport under load, not repeated memory registration -- if throughput
 * collapses as workers are added, registration churn is not the reason.
 *
 * CUDA driver API is declared inline: no CUDA toolkit headers and no nvcc are
 * needed on the client.
 *
 *   gcc -O2 -o bench_dfs_gpu bench_dfs_gpu.c -I$P/include -L$P/lib64 \
 *       -ldaos -ldfs -lgurt -lcart -luuid -lcuda -pthread -Wl,-rpath,$P/lib64
 *
 *   ./bench_dfs_gpu <pool> <cont> <arm> [chunk_MiB] [total_MiB] [workers]
 *
 * The data file is created on first use and reused afterwards, so the arms all
 * read identical bytes. Absolute numbers describe a warm server; the arms are
 * only compared against each other in the same session.
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
extern CUresult cuMemFree_v2(CUdeviceptr);
extern CUresult cuMemcpyHtoD_v2(CUdeviceptr, const void *, size_t);
extern CUresult cuCtxSynchronize(void);
extern CUresult cuMemHostAlloc(void **, size_t, unsigned int);
extern CUresult cuMemFreeHost(void *);

#define CU(call) do { CUresult _r = (call); if (_r != 0) {                      \
	fprintf(stderr, "CUDA %s = %d\n", #call, _r); exit(2); } } while (0)
#define DC(call) do { int _r = (call); if (_r != 0) {                           \
	fprintf(stderr, "%s = %d\n", #call, _r); exit(3); } } while (0)

enum arm { ARM_GPU, ARM_PINNEDCOPY, ARM_HOSTCOPY, ARM_PINNED, ARM_HOST };

static enum arm         arm;
static int              wants_h2d, wants_pinned;
static size_t           chunk;
static dfs_t           *dfs;
static CUcontext        cuctx;
static const char      *fname;
static pthread_barrier_t bar;

struct worker {
	pthread_t   th;
	long        id;
	size_t      off;	/* first byte this worker reads */
	size_t      len;	/* how much it reads */
	size_t      moved;
	dfs_obj_t  *obj;
	CUdeviceptr dptr;
	char       *hbuf;
	int         rc;
};

static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + 1e-9 * t.tv_nsec;
}

/* Create the data file if it is missing or too small. Written from host memory
 * on purpose: the write path is not what is being measured, and keeping it out
 * of the GPU path means a failure here cannot be mistaken for an arm result. */
static void ensure_file(const char *name, size_t total)
{
	dfs_obj_t   *obj = NULL;
	d_sg_list_t  sgl;
	d_iov_t      iov;
	struct stat  st;
	char        *buf;
	int          rc;

	rc = dfs_stat(dfs, NULL, name, &st);
	if (rc == 0 && (size_t)st.st_size >= total)
		return;

	fprintf(stderr, "creating %s (%zu MiB)...\n", name, total >> 20);
	rc = dfs_open(dfs, NULL, name, S_IFREG | 0644,
		      O_CREAT | O_RDWR | O_TRUNC, 0, 0, NULL, &obj);
	if (rc) { fprintf(stderr, "dfs_open(create) = %d\n", rc); exit(3); }

	if (posix_memalign((void **)&buf, 4096, chunk)) { perror("memalign"); exit(2); }
	for (size_t i = 0; i < chunk; i++)
		buf[i] = (char)(i * 31u);

	sgl.sg_nr = 1; sgl.sg_iovs = &iov;
	for (size_t off = 0; off < total; off += chunk) {
		sgl.sg_nr_out = 0;
		d_iov_set(&iov, buf, chunk);
		rc = dfs_write(dfs, obj, &sgl, off, NULL);
		if (rc) { fprintf(stderr, "dfs_write = %d\n", rc); exit(3); }
	}
	free(buf);
	dfs_release(obj);
}

static int read_one(struct worker *w, size_t off)
{
	d_sg_list_t sgl;
	d_iov_t     iov;
	daos_size_t got = 0;
	int         rc;

	sgl.sg_nr = 1; sgl.sg_nr_out = 0; sgl.sg_iovs = &iov;
	if (arm == ARM_GPU) {
		daos_mem_attr_t ma;

		memset(&ma, 0, sizeof(ma));
		ma.ma_mem_type  = DAOS_MEM_TYPE_CUDA;
		ma.ma_device_id = 0;
		d_iov_set(&iov, (void *)(uintptr_t)w->dptr, chunk);
		rc = dfs_read_gpu(dfs, w->obj, &sgl, off, &got, &ma);
	} else {
		d_iov_set(&iov, w->hbuf, chunk);
		rc = dfs_read(dfs, w->obj, &sgl, off, &got, NULL);
		if (rc == 0 && wants_h2d)
			CU(cuMemcpyHtoD_v2(w->dptr, w->hbuf, chunk));
	}
	if (rc)
		return rc;
	if (got != chunk) {
		fprintf(stderr, "short read at %zu: %zu of %zu\n",
			off, (size_t)got, chunk);
		return -1;
	}
	w->moved += got;
	return 0;
}

static void *worker_main(void *a)
{
	struct worker *w = a;

	/* Every thread needs the context bound before it touches device memory
	 * or the copy engine; cuCtxCreate only binds the creating thread. */
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
		memset(w->hbuf, 0, chunk);	/* fault it in before timing */
	}

	/* One untimed chunk so connection setup and first-touch registration do
	 * not land inside the measured interval. */
	w->rc = read_one(w, w->off);
	w->moved = 0;

ready:
	pthread_barrier_wait(&bar);
	if (w->rc)
		return NULL;

	for (size_t o = w->off; o < w->off + w->len; o += chunk) {
		w->rc = read_one(w, o);
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
	size_t      total;

	chunk = (size_t)(argc > 4 ? atol(argv[4]) : 32) << 20;
	total = (size_t)(argc > 5 ? atol(argv[5]) : 8192) << 20;

	if (!strcmp(armnm, "gpu"))              arm = ARM_GPU;
	else if (!strcmp(armnm, "pinnedcopy"))  arm = ARM_PINNEDCOPY;
	else if (!strcmp(armnm, "hostcopy"))    arm = ARM_HOSTCOPY;
	else if (!strcmp(armnm, "pinned"))      arm = ARM_PINNED;
	else if (!strcmp(armnm, "host"))        arm = ARM_HOST;
	else {
		fprintf(stderr,
			"arm must be gpu|pinnedcopy|hostcopy|pinned|host\n");
		return 1;
	}
	wants_h2d    = (arm == ARM_PINNEDCOPY) || (arm == ARM_HOSTCOPY);
	wants_pinned = (arm == ARM_PINNEDCOPY) || (arm == ARM_PINNED);

	if (nw < 1 || nw > 256) { fprintf(stderr, "workers out of range\n"); return 1; }
	total -= total % chunk;
	/* Give every worker a whole number of chunks so no slice straddles one. */
	size_t chunks_per = (total / chunk) / (size_t)nw;
	if (chunks_per == 0) {
		fprintf(stderr, "%d workers need at least %d chunks\n", nw, nw);
		return 1;
	}
	size_t slice = chunks_per * chunk;
	total = slice * (size_t)nw;

	daos_handle_t poh, coh;
	CUdevice      dev;

	CU(cuInit(0));
	CU(cuDeviceGet(&dev, 0));
	CU(cuCtxCreate_v2(&cuctx, 0, dev));

	DC(daos_init());
	DC(daos_pool_connect(pool, NULL, DAOS_PC_RW, &poh, NULL, NULL));
	DC(daos_cont_open(poh, cont, DAOS_COO_RW, &coh, NULL, NULL));
	DC(dfs_mount(poh, coh, O_RDWR, &dfs));

	static char name[64];
	snprintf(name, sizeof(name), "bench_%zuMiB", total >> 20);
	fname = name;
	ensure_file(name, total);

	struct worker *w = calloc((size_t)nw, sizeof(*w));
	if (!w) { perror("calloc"); return 2; }

	pthread_barrier_init(&bar, NULL, (unsigned)nw + 1);
	for (long i = 0; i < nw; i++) {
		w[i].id  = i;
		w[i].off = (size_t)i * slice;
		w[i].len = slice;
		if (pthread_create(&w[i].th, NULL, worker_main, &w[i])) {
			perror("pthread_create");
			return 2;
		}
	}

	pthread_barrier_wait(&bar);		/* all set up; start the clock */
	double t0 = now();
	for (int i = 0; i < nw; i++)
		pthread_join(w[i].th, NULL);
	double dt = now() - t0;

	size_t moved = 0;
	int    fails = 0;
	for (int i = 0; i < nw; i++) {
		moved += w[i].moved;
		if (w[i].rc)
			fails++;
	}
	if (fails) {
		fprintf(stderr, "%d of %d workers failed\n", fails, nw);
		return 4;
	}

	printf("arm=%-10s workers=%-3d chunk=%zuMiB total=%zuMiB secs=%.3f "
	       "GB/s=%.2f chunk_ms=%.3f\n",
	       armnm, nw, chunk >> 20, moved >> 20, dt, moved / dt / 1e9,
	       1e3 * dt / (double)(slice / chunk));

	for (int i = 0; i < nw; i++) {
		if (w[i].dptr) CU(cuMemFree_v2(w[i].dptr));
		if (w[i].hbuf) {
			if (wants_pinned)
				CU(cuMemFreeHost(w[i].hbuf));
			else
				free(w[i].hbuf);
		}
		if (w[i].obj) dfs_release(w[i].obj);
	}
	free(w);
	dfs_umount(dfs);
	daos_cont_close(coh, NULL);
	daos_pool_disconnect(poh, NULL);
	daos_fini();
	return 0;
}
