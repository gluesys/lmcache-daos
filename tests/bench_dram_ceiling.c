/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Gluesys Co., Ltd. */
/*
 * Host DRAM bandwidth ceiling -- the denominator for every "DRAM bytes per
 * byte delivered" claim in gpudirect/README.md.
 *
 * Why this exists: the first ceiling estimate (136.6 GB/s) came from a simple
 * unrolled read loop. That measures the loop, not the memory system. Every
 * conclusion about whether staging is feasible at 8 GPUs divides by this
 * number, so it has to be measured with something that actually saturates DRAM.
 *
 * The important property is not that this is STREAM, it is that it is read
 * with the SAME instrument as the load-side numbers:
 *
 *     perf stat -a -e uncore_imc/cas_count_read/,uncore_imc/cas_count_write/
 *
 * STREAM's own accounting counts only the array bytes the kernel names. On x86
 * with ordinary stores a Copy also pulls the destination line in (RFO), so the
 * real DRAM traffic is 3 array-sizes where STREAM counts 2. Reporting only the
 * STREAM number would understate the ceiling by 1.5x -- and understating the
 * denominator is what makes staging look infeasible. So this prints its own
 * counted bytes, and the wrapper script prints the IMC bytes next to them.
 * Use the IMC number as the ceiling. They should differ by the RFO factor and
 * nothing else; if they differ by more, do not trust either.
 *
 * build:
 *   gcc -O3 -march=native -fopenmp -o bench_dram_ceiling bench_dram_ceiling.c
 *
 * run (one kernel per invocation, so perf attribution stays clean):
 *   ./bench_dram_ceiling <kernel> [array_MiB] [timed_iters]
 *     kernel: read | copy | scale | add | triad
 *
 * timed_iters == 0 means "do the NUMA first-touch and one warm-up iteration,
 * then stop". That run exists so the wrapper can subtract its IMC byte count
 * from a full run's and be left with the timed iterations alone. Both runs
 * execute an identical init and warm-up, so the subtraction is exact in bytes.
 * Do not extend the subtraction to time: separate processes vary enough that
 * differencing their wall clocks produces bandwidths above the DIMMs' peak.
 *
 * NUMA: pin explicitly, because the answer differs and both answers matter.
 *   numactl -N0 -m0 ./bench_dram_ceiling triad   <- one socket
 *   ./bench_dram_ceiling triad                   <- both sockets (node ceiling)
 * An 8-GPU host spreads its staging buffers over both sockets, so the node
 * number is the one to divide by -- but only if the buffers are actually
 * spread. If they all land on the NIC's socket, the single-socket number is
 * the real ceiling and it is roughly half.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <omp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>

/* Arrays must be far larger than L3 (Xeon 8558: 260 MB) or this measures
 * cache. 1 GiB each is ~4x that with room to spare. */
#define DEFAULT_MIB 1024
#define DEFAULT_ITERS 10

static double *a, *b, *c;
static size_t n; /* elements per array */

static double now(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec * 1e-9;
}

/* Huge pages keep the TLB out of the result. Fall back quietly -- on a host
 * without enough free contiguous memory the plain mmap is still valid, just
 * slightly slower, and the wrapper prints which one was used. */
static double *alloc_arr(size_t bytes, int *huge)
{
	void *p = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		       MAP_PRIVATE | MAP_ANONYMOUS | MAP_HUGETLB, -1, 0);
	if (p != MAP_FAILED) {
		*huge = 1;
		return p;
	}
	p = mmap(NULL, bytes, PROT_READ | PROT_WRITE,
		 MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED) {
		fprintf(stderr, "mmap %zu: %s\n", bytes, strerror(errno));
		exit(1);
	}
	*huge = 0;
	return p;
}

int main(int argc, char **argv)
{
	const char *kern = argc > 1 ? argv[1] : "triad";
	size_t mib = argc > 2 ? strtoull(argv[2], NULL, 10) : DEFAULT_MIB;
	/* number of TIMED iterations; one warm-up always runs on top */
	int timed = argc > 3 ? atoi(argv[3]) : DEFAULT_ITERS;
	int iters = timed + 1;
	size_t bytes = mib * 1024UL * 1024UL;
	int huge_a, huge_b, huge_c, nthr;
	double best = 0.0, worst = 1e30, sum_t = 0.0;
	double counted; /* bytes this kernel is credited with, STREAM-style */
	volatile double sink = 0.0;

	n = bytes / sizeof(double);
	a = alloc_arr(bytes, &huge_a);
	b = alloc_arr(bytes, &huge_b);
	c = alloc_arr(bytes, &huge_c);

	/* First touch in the same parallel shape the kernels use, so pages land
	 * on the node whose thread will read them. Without this every page ends
	 * up on the initialising thread's node and a two-socket run measures one
	 * socket plus interconnect. */
#pragma omp parallel for schedule(static)
	for (size_t i = 0; i < n; i++) {
		a[i] = 1.0;
		b[i] = 2.0;
		c[i] = 0.0;
	}

#pragma omp parallel
	{
#pragma omp master
		nthr = omp_get_num_threads();
	}

	if (!strcmp(kern, "read") || !strcmp(kern, "copy") ||
	    !strcmp(kern, "scale"))
		counted = (double)bytes * (!strcmp(kern, "read") ? 1 : 2);
	else
		counted = (double)bytes * 3;

	for (int it = 0; it < iters; it++) {
		double t0 = now(), t1;

		if (!strcmp(kern, "read")) {
			double s = 0.0;
#pragma omp parallel for schedule(static) reduction(+ : s)
			for (size_t i = 0; i < n; i++)
				s += a[i];
			sink = s;
		} else if (!strcmp(kern, "copy")) {
#pragma omp parallel for schedule(static)
			for (size_t i = 0; i < n; i++)
				c[i] = a[i];
		} else if (!strcmp(kern, "scale")) {
#pragma omp parallel for schedule(static)
			for (size_t i = 0; i < n; i++)
				b[i] = 3.0 * c[i];
		} else if (!strcmp(kern, "add")) {
#pragma omp parallel for schedule(static)
			for (size_t i = 0; i < n; i++)
				c[i] = a[i] + b[i];
		} else if (!strcmp(kern, "triad")) {
#pragma omp parallel for schedule(static)
			for (size_t i = 0; i < n; i++)
				a[i] = b[i] + 3.0 * c[i];
		} else {
			fprintf(stderr, "unknown kernel: %s\n", kern);
			return 2;
		}

		t1 = now();
		/* Drop the first iteration: it pays for page-table walks and
		 * whatever the last kernel left in cache. */
		if (it == 0)
			continue;
		double gbs = counted / (t1 - t0) / 1e9;

		sum_t += t1 - t0;
		if (gbs > best)
			best = gbs;
		if (gbs < worst)
			worst = gbs;
	}

	printf("kernel=%s threads=%d array_MiB=%zu timed_iters=%d huge=%d%d%d\n",
	       kern, nthr, mib, timed, huge_a, huge_b, huge_c);
	/* timed_s and timed_counted_GB cover exactly the timed iterations, so
	 * the wrapper can pair them with an IMC byte delta. */
	if (timed > 0)
		printf("counted_GB=%.3f best_GBs=%.1f avg_GBs=%.1f "
		       "worst_GBs=%.1f timed_counted_GB=%.3f timed_s=%.6f "
		       "sink=%.1f\n",
		       counted / 1e9, best, counted * timed / sum_t / 1e9,
		       worst, counted * timed / 1e9, sum_t, sink);
	else
		printf("counted_GB=%.3f best_GBs=0.0 avg_GBs=0.0 worst_GBs=0.0 "
		       "timed_counted_GB=0.000 timed_s=0.000000 sink=%.1f\n",
		       counted / 1e9, sink);
	return 0;
}
