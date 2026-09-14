/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Gluesys Co., Ltd. */
/*
 * obj_latency.c -- how much of a small-object read is fixed cost, on the raw
 * object API rather than DFS.
 *
 * doc/LAYERWISE-MEASUREMENT.md fitted the DFS path at
 *     t_eff(size) ~= 0.63 ms + 0.067 ms/MiB      (per object, 16 threads)
 * and concluded that at 1 MiB objects 90% of the time is the fixed term. That
 * measurement could not separate "DAOS costs this much per object" from "DFS
 * costs this much per file", because every read went through a directory
 * lookup and a dc_array object. This does the same shape of work with neither.
 *
 * Layout: one object (class S16, so dkeys spread over the 16 targets the way
 * DFS spreads file chunks), dkey = chunk index, akey = layer index, each akey
 * a DAOS_IOD_ARRAY of `-s` bytes. That is the dkey/akey layout
 * doc/lmcache-mp-l2-assessment.md said we would need for a real batch RPC.
 *
 * Modes, all reading the identical bytes:
 *   sep    one daos_obj_fetch per (chunk, layer)  -- what layerwise does today
 *   batch  one daos_obj_fetch per chunk, all layers as N iods in one RPC
 *   one    one daos_obj_fetch per chunk, a single akey holding every layer
 *          -- the shape of today's non-layerwise 40 MiB object
 *   dfs    the control: one dfs_sys file per (chunk, layer) in a flat
 *          namespace, which is exactly what connector.py does today. Needs
 *          -C2 <posix container>. This is the arm that isolates "DFS costs
 *          this much per file" from "DAOS costs this much per object",
 *          since everything else in the process is identical.
 *
 * 'sep' vs 'batch' is the number this was written for: same bytes, same
 * placement, only the RPC count differs.
 *
 *   -p POOL -c CONT   plain (non-POSIX) container
 *   -o CLASS          object class (default S16)
 *   -t N              threads (default 16, to match the connector pool)
 *   -L N              layers per chunk (default 40, Qwen3-14B)
 *   -C N              chunks (default 120, = 30720 tokens at chunk_size 256)
 *   -s BYTES          bytes per layer (default 1048576)
 *   -r N              timed rounds, best is reported (default 3)
 *   -m MODE           sep | batch | one | dfs | all (default all)
 *   -P CONT           POSIX container, enables the dfs arm
 *
 * Build:
 *   gcc -O2 -pthread -o obj_latency obj_latency.c \
 *       -I$DAOS/include -L$DAOS/lib64 -ldaos -ldaos_common -lgurt \
 *       -Wl,-rpath,$DAOS/lib64
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <stddef.h>
#include <sys/stat.h>
#include <fcntl.h>

#include <daos.h>
#include <daos_obj_class.h>
#include <daos_fs_sys.h>

static char  *g_pool = "attr1", *g_cont = "objbench", *g_oc = "S16";
static char  *g_pcont = NULL;   /* POSIX container for the dfs arm */
static dfs_sys_t *g_dfs;
static daos_handle_t g_pcoh;
/* Buffers are allocated once and reused by every phase. Allocating per
 * phase churns addresses, which defeats the fabric MR cache and makes
 * NA_Mem_register() fail partway through the DFS arm. A real client holds
 * its staging buffers for the process lifetime, so this is also the
 * honest shape. */
static char *g_buf[256];
static char  *g_mode = "all";
static int    g_threads = 16, g_layers = 40, g_chunks = 120, g_rounds = 3;
static int    g_depth   = 1;   /* eq arm: requests in flight per EQ */
static size_t g_lsize = 1ul << 20;

static daos_handle_t g_poh, g_coh, g_oh;
static daos_obj_id_t g_oid;

#define AKEY_LEN 8
static void akey_name(char *buf, int layer) { snprintf(buf, AKEY_LEN, "L%03d", layer); }

static double now(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

#define CHK(rc, what)                                                          \
	do {                                                                   \
		if ((rc) != 0) {                                               \
			fprintf(stderr, "%s failed: %d (%s)\n", (what), (rc),   \
				d_errstr(rc));                                 \
			exit(1);                                               \
		}                                                              \
	} while (0)

/* One fetch/update covering `nak` akeys starting at `ak0`, or the ALL akey. */
static int io_chunk(int chunk, int ak0, int nak, char *buf, bool write, bool all_akey)
{
	daos_key_t  dkey;
	uint64_t    dk = chunk;
	daos_iod_t  iods[64];
	d_sg_list_t sgls[64];
	d_iov_t     iovs[64];
	daos_recx_t recxs[64];
	char        names[64][AKEY_LEN];
	int         n = all_akey ? 1 : nak;

	d_iov_set(&dkey, &dk, sizeof(dk));
	for (int i = 0; i < n; i++) {
		size_t sz = all_akey ? g_lsize * g_layers : g_lsize;
		size_t off = all_akey ? 0 : (size_t)i * g_lsize;

		if (all_akey)
			snprintf(names[i], AKEY_LEN, "ALL");
		else
			akey_name(names[i], ak0 + i);

		memset(&iods[i], 0, sizeof(iods[i]));
		d_iov_set(&iods[i].iod_name, names[i], strlen(names[i]));
		iods[i].iod_type  = DAOS_IOD_ARRAY;
		iods[i].iod_size  = 1;
		iods[i].iod_nr    = 1;
		recxs[i].rx_idx   = 0;
		recxs[i].rx_nr    = sz;
		iods[i].iod_recxs = &recxs[i];

		d_iov_set(&iovs[i], buf + off, sz);
		sgls[i].sg_nr     = 1;
		sgls[i].sg_nr_out = 0;
		sgls[i].sg_iovs   = &iovs[i];
	}
	if (write)
		return daos_obj_update(g_oh, DAOS_TX_NONE, 0, &dkey, n, iods, sgls, NULL);
	return daos_obj_fetch(g_oh, DAOS_TX_NONE, 0, &dkey, n, iods, sgls, NULL, NULL);
}

static int dfs_io_one(int chunk, int layer, char *buf, bool write)
{
	char       path[64];
	dfs_obj_t *obj = NULL;
	daos_size_t n = g_lsize;
	d_iov_t     iov;
	d_sg_list_t sgl;
	int         rc;

	snprintf(path, sizeof(path), "/c%05d_l%03d", chunk, layer);
	rc = dfs_sys_open(g_dfs, path, S_IFREG | 0644,
			  write ? O_CREAT | O_RDWR : O_RDONLY, 0, 0, NULL, &obj);
	if (rc != 0)
		return rc;

	d_iov_set(&iov, buf, g_lsize);
	sgl.sg_nr = 1; sgl.sg_nr_out = 0; sgl.sg_iovs = &iov;

	if (write)
		rc = dfs_sys_write(g_dfs, obj, &sgl, 0, &n, NULL);
	else
		rc = dfs_sys_read(g_dfs, obj, &sgl, 0, &n, NULL);

	dfs_sys_close(obj);
	return rc;
}

struct job {
	int  lo, hi;       /* chunk range */
	int  mode;         /* 0 sep, 1 batch, 2 one */
	bool write;
	char *buf;
};

static void *worker(void *arg)
{
	struct job *j = arg;

	for (int c = j->lo; c < j->hi; c++) {
		int rc;

		if (j->mode == 0) {
			for (int l = 0; l < g_layers; l++) {
				rc = io_chunk(c, l, 1, j->buf + (size_t)l * g_lsize,
					      j->write, false);
				CHK(rc, "obj io (sep)");
			}
		} else if (j->mode == 1) {
			rc = io_chunk(c, 0, g_layers, j->buf, j->write, false);
			CHK(rc, "obj io (batch)");
		} else if (j->mode == 2) {
			rc = io_chunk(c, 0, 1, j->buf, j->write, true);
			CHK(rc, "obj io (one)");
		} else {
			/* One buffer per thread, reused for every layer. The
			 * connector reads each chunk into one MemoryObj slot the
			 * same way, and slicing a 40 MiB allocation instead
			 * makes every layer a distinct region to register, which
			 * exhausts the fabric's memory registrations partway in
			 * (NA_Mem_register failure -> DER_HG_FATAL). */
			for (int l = 0; l < g_layers; l++) {
				rc = dfs_io_one(c, l, j->buf, j->write);
				CHK(rc, "dfs io");
			}
		}
	}
	return NULL;
}

/* ------------------------------------------------------------------ eq arm
 *
 * Why this exists: doc/DESIGN-AND-VALIDATION.md rejected the event queue on a
 * sweep that measured 1 EQ capping at ~12.5 GB/s and 16 EQs collapsing to
 * 2.74, against 34-35 GB/s for blocking threads. That sweep ran on the DFS
 * async path through the Python ctypes binding with 28 MiB reads -- a
 * different API, a different language and a different request shape from what
 * the NIXL plugin does. The mechanism it blamed (eq_progress_cb serialising
 * submit and completion under eqx_lock) lives in libdaos and does not care
 * about any of that, so the rejection is plausible here but not established.
 *
 * doc/FAILURE-MODES.md is why it matters: blocking loses 16/16 threads to a
 * dead engine and the event queue loses none. If the throughput gap does not
 * reproduce in this shape, that trade disappears.
 *
 * The variable is NOT thread count. It is EQ count x in-flight depth: one
 * thread per EQ, each keeping `depth` requests outstanding. K=16,D=1 is the
 * old sweep's worst point and K=1,D=16 its best, in the same harness as the
 * blocking arm for once.
 */
struct eqslot {
	daos_event_t ev;
	daos_iod_t   iods[64];
	daos_recx_t  recxs[64];
	d_sg_list_t  sgls[64];
	d_iov_t      iovs[64];
	char         names[64][AKEY_LEN];
	uint64_t     dk;
	daos_key_t   dkey;
	char        *buf;
	bool         busy;
};

/* Build and launch one chunk into a slot. The slot owns every structure DAOS
 * will read asynchronously -- building them on the stack the way io_chunk()
 * does would hand libdaos pointers into a frame that returns immediately. */
static int eq_submit(struct eqslot *sl, daos_handle_t eq, int chunk, bool write)
{
	int n = g_layers, rc;

	sl->dk = chunk;
	d_iov_set(&sl->dkey, &sl->dk, sizeof(sl->dk));
	for (int i = 0; i < n; i++) {
		akey_name(sl->names[i], i);
		memset(&sl->iods[i], 0, sizeof(sl->iods[i]));
		d_iov_set(&sl->iods[i].iod_name, sl->names[i], strlen(sl->names[i]));
		sl->iods[i].iod_type  = DAOS_IOD_ARRAY;
		sl->iods[i].iod_size  = 1;
		sl->iods[i].iod_nr    = 1;
		sl->recxs[i].rx_idx   = 0;
		sl->recxs[i].rx_nr    = g_lsize;
		sl->iods[i].iod_recxs = &sl->recxs[i];

		d_iov_set(&sl->iovs[i], sl->buf + (size_t)i * g_lsize, g_lsize);
		sl->sgls[i].sg_nr     = 1;
		sl->sgls[i].sg_nr_out = 0;
		sl->sgls[i].sg_iovs   = &sl->iovs[i];
	}
	rc = daos_event_init(&sl->ev, eq, NULL);
	if (rc)
		return rc;
	rc = write ? daos_obj_update(g_oh, DAOS_TX_NONE, 0, &sl->dkey, n,
				     sl->iods, sl->sgls, &sl->ev)
		   : daos_obj_fetch(g_oh, DAOS_TX_NONE, 0, &sl->dkey, n,
				    sl->iods, sl->sgls, NULL, &sl->ev);
	if (rc) {
		daos_event_fini(&sl->ev);
		return rc;
	}
	sl->busy = true;
	return 0;
}

static void *eq_worker(void *arg)
{
	struct job    *j = arg;
	daos_handle_t  eq;
	struct eqslot *slots;
	int            depth = g_depth, next = j->lo, outstanding = 0, rc;

	rc = daos_eq_create(&eq);
	CHK(rc, "daos_eq_create");
	slots = calloc(depth, sizeof(*slots));
	if (!slots)
		exit(1);
	for (int i = 0; i < depth; i++) {
		slots[i].buf = malloc(g_lsize * g_layers);
		if (!slots[i].buf)
			exit(1);
		memset(slots[i].buf, 0x5a, g_lsize * g_layers);
	}

	while (next < j->hi || outstanding) {
		while (next < j->hi && outstanding < depth) {
			int i = 0;
			while (i < depth && slots[i].busy) i++;
			rc = eq_submit(&slots[i], eq, next++, j->write);
			CHK(rc, "eq submit");
			outstanding++;
		}
		daos_event_t *evp = NULL;
		int n = daos_eq_poll(eq, 1, DAOS_EQ_WAIT, 1, &evp);
		if (n < 0)
			CHK(n, "daos_eq_poll");
		if (n == 0)
			continue;
		CHK(evp->ev_error, "eq completion");
		/* evp points at the slot's embedded event, so the slot is
		 * recovered by offset rather than by searching. */
		struct eqslot *sl = (struct eqslot *)((char *)evp - offsetof(struct eqslot, ev));
		daos_event_fini(&sl->ev);
		sl->busy = false;
		outstanding--;
	}

	for (int i = 0; i < depth; i++)
		free(slots[i].buf);
	free(slots);
	daos_eq_destroy(eq, 0);
	return NULL;
}

static double run(int mode, bool write)
{
	pthread_t   th[256];
	struct job  jobs[256];
	int         per = (g_chunks + g_threads - 1) / g_threads;
	double      t0;

	for (int i = 0; i < g_threads; i++) {
		jobs[i].lo    = i * per;
		jobs[i].hi    = (i + 1) * per > g_chunks ? g_chunks : (i + 1) * per;
		if (jobs[i].lo > g_chunks) jobs[i].lo = g_chunks;
		jobs[i].mode  = mode;
		jobs[i].write = write;
		jobs[i].buf   = g_buf[i];
	}
	t0 = now();
	for (int i = 0; i < g_threads; i++)
		pthread_create(&th[i], NULL, mode == 4 ? eq_worker : worker, &jobs[i]);
	for (int i = 0; i < g_threads; i++)
		pthread_join(th[i], NULL);
	return now() - t0;
}

static void report(const char *name, double dt, long rpcs)
{
	double bytes = (double)g_chunks * g_layers * g_lsize;

	printf("%-6s  wall %8.1f ms | RPC %6ld | per-RPC %7.3f ms | "
	       "per-layer-obj %7.3f ms | %6.2f GB/s\n",
	       name, dt * 1e3, rpcs, dt * 1e3 / rpcs,
	       dt * 1e3 / ((double)g_chunks * g_layers), bytes / dt / 1e9);
	fflush(stdout);
}

int main(int argc, char **argv)
{
	int rc, opt;

	while ((opt = getopt(argc, argv, "p:c:o:m:t:L:C:s:r:P:d:")) != -1) {
		switch (opt) {
		case 'p': g_pool = optarg; break;
		case 'c': g_cont = optarg; break;
		case 'o': g_oc = optarg; break;
		case 'm': g_mode = optarg; break;
		case 'd': g_depth = atoi(optarg); break;
		case 't': g_threads = atoi(optarg); break;
		case 'L': g_layers = atoi(optarg); break;
		case 'C': g_chunks = atoi(optarg); break;
		case 's': g_lsize = strtoul(optarg, NULL, 0); break;
		case 'r': g_rounds = atoi(optarg); break;
		case 'P': g_pcont = optarg; break;
		default: fprintf(stderr, "bad option\n"); return 2;
		}
	}
	if (g_layers > 64) { fprintf(stderr, "-L max 64\n"); return 2; }
	if (g_threads > 256) { fprintf(stderr, "-t max 256\n"); return 2; }

	rc = daos_init();
	CHK(rc, "daos_init");
	rc = daos_pool_connect(g_pool, NULL, DAOS_PC_RW, &g_poh, NULL, NULL);
	CHK(rc, "daos_pool_connect");
	rc = daos_cont_open(g_poh, g_cont, DAOS_COO_RW, &g_coh, NULL, NULL);
	CHK(rc, "daos_cont_open");

	daos_oclass_id_t oc = daos_oclass_name2id(g_oc);
	if (oc == OC_UNKNOWN) { fprintf(stderr, "bad oclass %s\n", g_oc); return 2; }

	memset(&g_oid, 0, sizeof(g_oid));
	g_oid.lo = 0x10BE0CULL;
	rc = daos_obj_generate_oid(g_coh, &g_oid, DAOS_OT_MULTI_HASHED, oc, 0, 0);
	CHK(rc, "daos_obj_generate_oid");
	rc = daos_obj_open(g_coh, g_oid, DAOS_OO_RW, &g_oh, NULL);
	CHK(rc, "daos_obj_open");

	if (g_pcont) {
		rc = daos_cont_open(g_poh, g_pcont, DAOS_COO_RW, &g_pcoh, NULL, NULL);
		CHK(rc, "daos_cont_open (posix)");
		rc = dfs_sys_mount(g_poh, g_pcoh, O_RDWR, 0, &g_dfs);
		CHK(rc, "dfs_sys_mount");
	}

	for (int i = 0; i < g_threads; i++) {
		g_buf[i] = aligned_alloc(4096, g_lsize * g_layers);
		if (!g_buf[i]) { perror("alloc"); exit(1); }
		memset(g_buf[i], 0x5a, g_lsize * g_layers);
	}

	printf("pool=%s cont=%s oc=%s threads=%d layers=%d chunks=%d "
	       "layer=%zu B total=%.2f GiB\n",
	       g_pool, g_cont, g_oc, g_threads, g_layers, g_chunks, g_lsize,
	       (double)g_chunks * g_layers * g_lsize / (1024.0 * 1024 * 1024));


	/* Populate only the layouts this run will read. Doing all of them in one
	 * process is what first exposed the registration failure below, and it
	 * also makes each arm pay for the others' warm-up. */
	bool all = strcmp(g_mode, "all") == 0;
	if (!strcmp(g_mode, "eq") || all)
		printf("eq arm: %d EQs (one per thread) x depth %d = %d in flight\n",
		       g_threads, g_depth, g_threads * g_depth);
	/* The eq arm reads the same per-akey layout the batch arm does, so it
	 * needs the same populate pass -- otherwise it measures fetches of
	 * records that were never written and reports a throughput that is
	 * really the cost of returning nothing. */
	bool need_ak  = all || !strcmp(g_mode, "sep") || !strcmp(g_mode, "batch")
			|| !strcmp(g_mode, "eq");
	bool need_one = all || !strcmp(g_mode, "one");
	bool need_dfs = g_pcont && (all || !strcmp(g_mode, "dfs"));
	double w1 = need_ak  ? run(1, true) : 0;
	double w2 = need_one ? run(2, true) : 0;
	double w3 = need_dfs ? run(3, true) : 0;
	printf("populate: akeys %.1f ms, ALL %.1f ms, dfs %.1f ms\n",
	       w1 * 1e3, w2 * 1e3, w3 * 1e3);

	struct { const char *name; int mode; long rpcs; } arms[] = {
		{ "sep",   0, (long)g_chunks * g_layers },
		{ "batch", 1, (long)g_chunks },
		{ "one",   2, (long)g_chunks },
		{ "dfs",   3, (long)g_chunks * g_layers },
		{ "eq",    4, (long)g_chunks },
	};
	for (unsigned a = 0; a < sizeof(arms) / sizeof(arms[0]); a++) {
		if (strcmp(g_mode, "all") != 0 && strcmp(g_mode, arms[a].name) != 0)
			continue;
		if (arms[a].mode == 3 && !g_pcont)
			continue;
		double best = 1e30;
		for (int r = 0; r < g_rounds; r++) {
			double dt = run(arms[a].mode, false);
			if (dt < best) best = dt;
		}
		report(arms[a].name, best, arms[a].rpcs);
	}

	if (g_pcont) { dfs_sys_umount(g_dfs); daos_cont_close(g_pcoh, NULL); }
	daos_obj_close(g_oh, NULL);
	daos_cont_close(g_coh, NULL);
	daos_pool_disconnect(g_poh, NULL);
	daos_fini();
	return 0;
}
