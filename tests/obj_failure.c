/* SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Gluesys Co., Ltd.
 *
 * Does the raw object API come back when the engine dies?
 *
 * doc/FAILURE-MODES.md measured the DFS path: SIGKILL of daos_server during a
 * write left 16 of 16 threads inside dfs_sys_write, still there 16 minutes
 * later, spinning ~13 cores, and CRT_TIMEOUT=10 changed nothing. That is the
 * measured reason a wedged pool had to be detected from the outside.
 *
 * Switching to dkey/akey is attractive for a different reason -- the fixed cost
 * per object is 0.0137 ms against DFS's 0.63 ms -- but nobody has measured what
 * the low-level path does under the same fault, and "it is a different API" is
 * not evidence. This measures it.
 *
 * Two arms, because they are not the same question:
 *
 *   sync   daos_obj_update(..., NULL) -- blocking, exactly what the NIXL
 *          plugin does today (nixl/plugin/daos_backend.cpp). If this wedges
 *          like DFS, the raw API buys performance and changes nothing about
 *          failure.
 *
 *   async-abort  as async, but then daos_event_abort() + daos_event_fini(),
 *          which is what a real caller must do -- the event cannot simply be
 *          abandoned, since DAOS still holds a pointer to it. If abort blocks,
 *          the escape hatch only moved the block somewhere else.
 *
 *   async  daos_obj_update(..., &ev) into an event queue, then daos_eq_poll()
 *          with a timeout. THIS is the part DFS cannot do at all. If the poll
 *          returns while the RPC is still outstanding, the caller can give up
 *          on its own schedule -- which is the property the connector had to
 *          fake with a wedge detector.
 *
 * Reported per thread: did the call return, after how long, with what rc. The
 * main thread prints on a deadline and _exit()s, because threads stuck inside
 * libdaos cannot be joined and a harness that hangs cannot report a hang.
 *
 *   gcc -O2 -pthread -o obj_failure obj_failure.c \
 *       -I$DAOS/include -L$DAOS/lib64 -ldaos -ldaos_common -lgurt -luuid
 *
 *   ./obj_failure --pool kvpool --cont nixltest --arm sync  --kill-after 0.3
 *   ./obj_failure --pool kvpool --cont nixltest --arm async --kill-after 0.3
 *
 * DESTRUCTIVE: it SIGKILLs daos_server. Single-node test hosts only.
 */
#include <errno.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <daos.h>
#include <daos_obj_class.h>

#define AKEY_LEN 16
#define MAXTH    64

static char  *g_pool = "kvpool", *g_cont = "nixltest", *g_oc = "SX";
static char  *g_arm  = "sync";
static int    g_threads = 16, g_iters = 4000;
static size_t g_lsize = 8ul << 20;      /* 8 MiB per update -- big enough that
                                         * the kill lands inside one */
static double g_kill_after = 0.3, g_deadline = 120.0, g_poll_timeout = 5.0;

static daos_handle_t g_poh, g_coh, g_oh;
static daos_obj_id_t g_oid;

/* Per-thread outcome. Written by the worker, read by main while the worker may
 * still be stuck -- hence atomics and no join. */
struct slot {
	atomic_int    done;      /* 0 = still inside the call */
	atomic_int    rc;
	atomic_int    iters;     /* completed before the fault */
	double        t_enter;   /* when the call that is still running began */
	double        t_exit;
	atomic_int    gaveup;    /* async arm: poll timed out, caller walked away */
	double        t_abort;   /* async-abort arm: how long abort() itself took */
	double        t_fini;
};
static struct slot g_slot[MAXTH];
static atomic_int  g_killed;

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

/* One 8 MiB update under its own dkey, so threads never collide. */
static void setup_io(uint64_t dk, char *buf, daos_key_t *dkey, daos_iod_t *iod,
		     d_sg_list_t *sgl, d_iov_t *iov, daos_recx_t *recx, char *name)
{
	d_iov_set(dkey, &dk, sizeof(dk));          /* caller owns dk's storage */
	snprintf(name, AKEY_LEN, "A");
	memset(iod, 0, sizeof(*iod));
	d_iov_set(&iod->iod_name, name, strlen(name));
	iod->iod_type  = DAOS_IOD_ARRAY;
	iod->iod_size  = 1;
	iod->iod_nr    = 1;
	recx->rx_idx   = 0;
	recx->rx_nr    = g_lsize;
	iod->iod_recxs = recx;

	d_iov_set(iov, buf, g_lsize);
	sgl->sg_nr     = 1;
	sgl->sg_nr_out = 0;
	sgl->sg_iovs   = iov;
}

static void *worker(void *arg)
{
	int          id = (int)(intptr_t)arg;
	struct slot *s  = &g_slot[id];
	char        *buf = malloc(g_lsize);
	daos_handle_t eq = DAOS_HDL_INVAL;
	bool         abort_ev = strcmp(g_arm, "async-abort") == 0;
	bool         async = abort_ev || strcmp(g_arm, "async") == 0;

	if (!buf)
		return NULL;
	memset(buf, 0x5a, g_lsize);

	if (async) {
		int rc = daos_eq_create(&eq);
		if (rc) {
			atomic_store(&s->rc, rc);
			atomic_store(&s->done, 1);
			return NULL;
		}
	}

	for (int i = 0; i < g_iters; i++) {
		uint64_t     dk = (uint64_t)id * 1000000 + i;
		daos_key_t   dkey;
		daos_iod_t   iod;
		d_sg_list_t  sgl;
		d_iov_t      iov;
		daos_recx_t  recx;
		char         name[AKEY_LEN];
		int          rc;

		setup_io(dk, buf, &dkey, &iod, &sgl, &iov, &recx, name);
		s->t_enter = now();

		if (!async) {
			rc = daos_obj_update(g_oh, DAOS_TX_NONE, 0, &dkey, 1,
					     &iod, &sgl, NULL);
		} else {
			daos_event_t  ev;
			daos_event_t *evp = NULL;
			int           n;

			rc = daos_event_init(&ev, eq, NULL);
			if (rc)
				break;
			rc = daos_obj_update(g_oh, DAOS_TX_NONE, 0, &dkey, 1,
					     &iod, &sgl, &ev);
			if (rc == 0) {
				/* The whole point of this arm: the caller sets
				 * the deadline, not DAOS. A timeout here means
				 * the RPC is still outstanding and we walked
				 * away from it anyway -- which the blocking
				 * form cannot express. */
				n = daos_eq_poll(eq, 1,
						 (int64_t)(g_poll_timeout * 1e6),
						 1, &evp);
				if (n == 0) {
					/* The RPC is STILL OUTSTANDING here. The
					 * thread is free, the operation is not.
					 * `ev` lives on this stack frame, so
					 * simply walking away leaves DAOS with a
					 * pointer into a frame that is about to
					 * die -- which is why the abort arm
					 * exists: it measures whether the tidy
					 * version of this pattern is itself
					 * bounded, or just moves the block. */
					double ta = now();
					if (abort_ev) {
						daos_event_abort(&ev);
						s->t_abort = now() - ta;
						daos_event_fini(&ev);
						s->t_fini = now() - ta - s->t_abort;
					}
					atomic_store(&s->gaveup, 1);
					s->t_exit = now();
					atomic_store(&s->rc, -ETIMEDOUT);
					atomic_store(&s->done, 1);
					break;
				}
				rc = (n < 0) ? n : evp->ev_error;
			}
			daos_event_fini(&ev);
		}

		if (rc != 0) {
			s->t_exit = now();
			atomic_store(&s->rc, rc);
			atomic_store(&s->done, 1);
			break;
		}
		if (!atomic_load(&g_killed))
			atomic_fetch_add(&s->iters, 1);
	}
	if (!atomic_load(&s->done)) {
		s->t_exit = now();
		atomic_store(&s->done, 1);
	}
	return NULL;
}

static void *killer(void *arg)
{
	(void)arg;
	struct timespec ts = { (time_t)g_kill_after,
			       (long)((g_kill_after - (time_t)g_kill_after) * 1e9) };
	nanosleep(&ts, NULL);
	printf("-- SIGKILL daos_server --\n");
	fflush(stdout);
	if (system("pkill -9 -x daos_server") == -1)
		perror("pkill");
	atomic_store(&g_killed, 1);
	return NULL;
}

int main(int argc, char **argv)
{
	pthread_t th[MAXTH], kt;
	int       rc;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--pool") && i + 1 < argc)        g_pool = argv[++i];
		else if (!strcmp(argv[i], "--cont") && i + 1 < argc)   g_cont = argv[++i];
		else if (!strcmp(argv[i], "--arm") && i + 1 < argc)    g_arm = argv[++i];
		else if (!strcmp(argv[i], "--threads") && i + 1 < argc) g_threads = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--mib") && i + 1 < argc)    g_lsize = (size_t)atoi(argv[++i]) << 20;
		else if (!strcmp(argv[i], "--kill-after") && i + 1 < argc) g_kill_after = atof(argv[++i]);
		else if (!strcmp(argv[i], "--deadline") && i + 1 < argc)   g_deadline = atof(argv[++i]);
		else if (!strcmp(argv[i], "--poll-timeout") && i + 1 < argc) g_poll_timeout = atof(argv[++i]);
		else if (!strcmp(argv[i], "--oclass") && i + 1 < argc) g_oc = argv[++i];
		else { fprintf(stderr, "unknown arg: %s\n", argv[i]); return 2; }
	}
	if (g_threads > MAXTH) g_threads = MAXTH;

	printf("arm=%s pool=%s cont=%s threads=%d x %zu MiB kill-after=%.2fs "
	       "deadline=%.0fs%s\n", g_arm, g_pool, g_cont, g_threads,
	       g_lsize >> 20, g_kill_after, g_deadline,
	       strcmp(g_arm, "async") ? "" : " poll-timeout=see below");

	rc = daos_init();                                       CHK(rc, "daos_init");
	rc = daos_pool_connect(g_pool, NULL, DAOS_PC_RW, &g_poh, NULL, NULL);
	CHK(rc, "daos_pool_connect");
	rc = daos_cont_open(g_poh, g_cont, DAOS_COO_RW, &g_coh, NULL, NULL);
	CHK(rc, "daos_cont_open");

	daos_oclass_id_t oc = daos_oclass_name2id(g_oc);
	rc = daos_obj_generate_oid(g_coh, &g_oid, DAOS_OT_MULTI_HASHED, oc, 0, 0);
	CHK(rc, "daos_obj_generate_oid");
	rc = daos_obj_open(g_coh, g_oid, DAOS_OO_RW, &g_oh, NULL);
	CHK(rc, "daos_obj_open");

	double t0 = now();
	for (int i = 0; i < g_threads; i++)
		pthread_create(&th[i], NULL, worker, (void *)(intptr_t)i);
	pthread_create(&kt, NULL, killer, NULL);

	/* Poll rather than join: a thread inside libdaos never returns, and
	 * joining it would make this program the thing under test. */
	int done = 0;
	while (now() - t0 < g_deadline) {
		done = 0;
		for (int i = 0; i < g_threads; i++)
			done += atomic_load(&g_slot[i].done);
		if (done == g_threads)
			break;
		usleep(200000);
	}
	double elapsed = now() - t0;

	int stuck = 0, gaveup = 0, errored = 0, landed = 0;
	printf("\n  thr  state      after     rc\n");
	for (int i = 0; i < g_threads; i++) {
		struct slot *s = &g_slot[i];
		int d = atomic_load(&s->done), r = atomic_load(&s->rc);
		landed += atomic_load(&s->iters);
		if (!d) {
			stuck++;
			printf("  %3d  STUCK    %7.1fs    -        (still inside the call)\n",
			       i, now() - s->t_enter);
		} else if (atomic_load(&s->gaveup)) {
			gaveup++;
			if (s->t_abort > 0 || s->t_fini > 0)
				printf("  %3d  gave up  %7.2fs    poll timeout "
				       "(abort %.2fs, fini %.2fs)\n", i,
				       s->t_exit - s->t_enter, s->t_abort, s->t_fini);
			else
				printf("  %3d  gave up  %7.2fs    poll timeout\n",
				       i, s->t_exit - s->t_enter);
		} else {
			errored += (r != 0);
			printf("  %3d  returned %7.2fs    %d %s\n", i,
			       s->t_exit - s->t_enter, r, r ? d_errstr(r) : "");
		}
	}

	printf("\n  %-46s %d / %d\n", "threads still inside the call", stuck, g_threads);
	printf("  %-46s %d\n", "returned with an error", errored);
	printf("  %-46s %d\n", "gave up on their own (async only)", gaveup);
	printf("  %-46s %d\n", "updates completed before the kill", landed);
	printf("  %-46s %.1fs\n", "elapsed", elapsed);

	if (stuck)
		printf("\n  VERDICT: the low-level path wedges like DFS -- %d/%d threads\n"
		       "           are unreclaimable, same as doc/FAILURE-MODES.md.\n",
		       stuck, g_threads);
	else if (gaveup)
		printf("\n  VERDICT: the caller set the deadline and kept its threads.\n"
		       "           DFS has no equivalent -- this is a real difference.\n");
	else
		printf("\n  VERDICT: every call returned an error on its own.\n");

	fflush(stdout);
	/* Not daos_fini(): with threads stuck inside libdaos it would hang, and
	 * this process exists to be wrecked. */
	_exit(stuck ? 1 : 0);
}
