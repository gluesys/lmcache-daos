/* CXL / Device-DAX raw read bandwidth, multi-threaded.
 *
 * Why C and not Python: the copy must run without the GIL. A ctypes/memoryview
 * version collapses under 16 threads and reports a number that describes the
 * interpreter, not the device (we measured 32.8 -> 5.8 GB/s that way).
 *
 * Why a DRAM control is built in: a CXL read number is only meaningful next to
 * a DRAM number taken by the *same* harness. Our first version had broken
 * timing and printed millions of GB/s; the control is what makes that obvious.
 * Always run `anon` first and sanity-check it before quoting a dax number.
 *
 *   gcc -O2 -pthread -o bench_dax_bw bench_dax_bw.c
 *
 *   ./bench_dax_bw anon         32 16      # DRAM control, 32 GiB, 16 threads
 *   ./bench_dax_bw /dev/dax0.0  32 16      # CXL, read-only (safe)
 *   MODE=load ./bench_dax_bw /dev/dax0.0 32 16
 *   numactl --cpunodebind=0 ./bench_dax_bw /dev/dax0.0 32 16
 *
 * MODE=copy (default) copies into a per-thread DRAM buffer, so the reported
 * figure counts bytes read while an equal write stream runs alongside it.
 * MODE=load only sums the source with unrolled loads and never stores, which
 * is what MLC-style tools report. On the CZ120 the two agree to within 0.5%
 * (11.76 vs 11.81 GB/s), which is how we ruled out the destination write as
 * the limiter -- but quote which mode you used, because on a healthy DRAM node
 * they differ a lot (106 copy vs 192 load).
 *
 * READ-ONLY BY DEFAULT. The device is opened O_RDONLY and mapped PROT_READ, so
 * it cannot damage data that already lives on the dax device. This matters:
 * DAOS can own a dax device as VOS SCM (`class: cxl` / `cxl_dax_path`), and
 * writing to it under a running daos_engine destroys metadata.
 *
 * PREFILL=1 opens the device read-write and stamps a pattern over the whole
 * mapped range first. Only use it on a dax device that nothing else owns --
 * it is destructive. It exists because a freshly-onlined devdax region may
 * read as zeroes, and some controllers short-circuit reads of unwritten media.
 *
 * Timing: threads are released by a barrier, and the interval ends at
 * pthread_join. Do NOT use a second barrier to close the interval -- reusing
 * the barrier is what produced the bogus 15675176 GB/s in the first version.
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

static const char *src_base;
static size_t per, chunk = 32UL << 20;
static int nthr, iters;
static pthread_barrier_t bar;
static volatile unsigned long sink;

static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + 1e-9 * t.tv_nsec;
}

static int load_mode;			/* MODE=load: sum the source, never store */

static void *worker_copy(void *a)
{
	long id = (long)a;
	char *dst = aligned_alloc(4096, chunk);
	if (!dst) {
		fprintf(stderr, "thread %ld: dst alloc failed\n", id);
		exit(2);
	}
	memset(dst, 1, chunk);			/* fault the destination into DRAM */
	const char *base = src_base + (size_t)id * per;

	pthread_barrier_wait(&bar);
	for (int it = 0; it < iters; it++)
		for (size_t off = 0; off < per; off += chunk) {
			memcpy(dst, base + off, chunk);
			sink += (unsigned long)dst[0];	/* keep the copy alive */
		}
	free(dst);
	return NULL;
}

static void *worker_load(void *a)
{
	long id = (long)a;
	const unsigned long *p = (const unsigned long *)(src_base + (size_t)id * per);
	size_t n = per / sizeof(*p);
	unsigned long s0 = 0, s1 = 0, s2 = 0, s3 = 0, s4 = 0, s5 = 0, s6 = 0, s7 = 0;

	pthread_barrier_wait(&bar);
	for (int it = 0; it < iters; it++)
		for (size_t i = 0; i + 8 <= n; i += 8) {
			s0 += p[i];     s1 += p[i + 1]; s2 += p[i + 2]; s3 += p[i + 3];
			s4 += p[i + 4]; s5 += p[i + 5]; s6 += p[i + 6]; s7 += p[i + 7];
		}
	sink += s0 + s1 + s2 + s3 + s4 + s5 + s6 + s7;	/* keep the loads alive */
	return NULL;
}

int main(int argc, char **argv)
{
	const char *dev = argc > 1 ? argv[1] : "/dev/dax0.0";
	size_t total = (size_t)(argc > 2 ? atol(argv[2]) : 32) * (1UL << 30);
	nthr = argc > 3 ? atoi(argv[3]) : 8;
	iters = argc > 4 ? atoi(argv[4]) : 2;
	const char *pf = getenv("PREFILL");
	int prefill = pf && (*pf == '1' || *pf == 'y' || *pf == 'Y');
	const char *md = getenv("MODE");
	load_mode = md && !strcmp(md, "load");

	if (nthr < 1 || nthr > 512) {
		fprintf(stderr, "threads out of range\n");
		return 1;
	}

	if (!strcmp(dev, "anon")) {
		char *p = mmap(NULL, total, PROT_READ | PROT_WRITE,
			       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
		if (p == MAP_FAILED) {
			perror("mmap anon");
			return 1;
		}
		for (size_t o = 0; o < total; o += 4096)
			p[o] = (char)(o >> 12);
		src_base = p;
	} else {
		/* Read-only unless PREFILL was asked for -- see header comment. */
		int fd = open(dev, prefill ? O_RDWR : O_RDONLY);
		if (fd < 0) {
			perror("open");
			return 1;
		}
		char *p = mmap(NULL, total, prefill ? (PROT_READ | PROT_WRITE) : PROT_READ,
			       MAP_SHARED, fd, 0);
		if (p == MAP_FAILED) {
			perror("mmap dax");
			return 1;
		}
		if (prefill) {
			fprintf(stderr, "PREFILL=1: writing over %s -- destructive\n", dev);
			for (size_t o = 0; o < total; o += 4096)
				p[o] = (char)(o >> 12);
		}
		src_base = p;
	}

	per = load_mode ? ((total / nthr) & ~(size_t)4095)
			: ((total / nthr) & ~(chunk - 1));
	if (per == 0) {
		fprintf(stderr, "per-thread span smaller than one %zu MiB chunk; "
			"use a larger size or fewer threads\n", chunk >> 20);
		return 1;
	}

	pthread_barrier_init(&bar, NULL, nthr + 1);
	pthread_t th[512];
	for (long i = 0; i < nthr; i++)
		if (pthread_create(&th[i], NULL,
				   load_mode ? worker_load : worker_copy, (void *)i)) {
			perror("pthread_create");
			return 1;
		}

	pthread_barrier_wait(&bar);
	double t0 = now();
	for (int i = 0; i < nthr; i++)
		pthread_join(th[i], NULL);
	double dt = now() - t0;

	double bytes = (double)per * nthr * iters;
	printf("%-4s %-12s threads=%-3d read=%6.1fGiB  %6.2f GB/s%s\n",
	       load_mode ? "LOAD" : "COPY", dev, nthr, bytes / 1073741824.0,
	       bytes / dt / 1e9, prefill ? "  (prefilled)" : "");
	return 0;
}
