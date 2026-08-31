/*
 * dfs_integrity.c -- self-contained reproducer for silent data corruption in
 * DAOS 2.9.100's DFS read path.
 *
 * C, pthreads, dfs_sys only: no Python, no ctypes, no LMCache, no GPU. An
 * upstream reader should not have to trust any of our stack to reproduce this,
 * and the Python version (tests/test_rawio_integrity.py) cannot run enough
 * trials to bound a ~1% per-read failure rate (§6 of
 * gpudirect/DAOS-CONCURRENT-READ-CORRUPTION.md).
 *
 * PAYLOAD FORMAT -- the reason this version can say more than "bytes differ".
 * Every 8-byte word is a self-describing tag:
 *
 *     (tid << 56) | (round << 48) | payload_offset
 *
 * so a wrong region names its own origin. The earlier reproducer used
 * base[i] = (i * 31 + tid * 101) & 0xff, in which a foreign thread's data is
 * mathematically indistinguishable from the same thread's data at a shifted
 * offset -- both shift the byte value by a constant mod 256, which is where
 * the "own pattern shifted by 245" readings came from. Tagged words separate
 * four faults that all previously read as "no pattern":
 *
 *     wrong tid     another object's data was served for this one
 *     wrong round   a stale version of this object's data
 *     wrong offset  this object's data, from the wrong place
 *     zeros         a hole: nothing was returned for that range
 *
 * VERIFICATION needs no knowledge of the write history: a correct object has
 * every word tagged with its own tid, its own offset, and one single round
 * number. So -Q can audit objects left behind by any earlier run.
 *
 *   -s MiB      object size            (default 28)
 *   -t N        threads                (default 16)
 *   -r N        rounds per thread      (default 13)
 *   -o BYTES    payload offset         (default 36 = the connector's header;
 *                                       0 skips the header write)
 *   -m MODE     burst | loop           (default burst: reads released together)
 *   -f FLAGS    dfs_sys sflags         (0 = cache+lock on, 1 = NO_CACHE)
 *   -H 0|1      one dfs_sys handle per thread instead of one shared
 *   -d MS       pause between the writes and the reads
 *   -F 0|1      write a fresh object every round instead of reusing paths
 *   -W          write every object single-threaded first (no concurrency)
 *   -N          threads write only   (isolates the write path)
 *   -M          threads read only    (isolates the read path; pair with -W)
 *   -Q          verify existing objects only, write nothing
 *   -V N        quiet verify passes at the end (default 1)
 *   -p POOL -c CONT
 *
 * Exit status: 0 everything correct, 1 corruption seen, 2 usage/DAOS error.
 * A single clean run is NOT evidence of absence: at 1% per read, 208 reads
 * pass 12% of the time. Read the printed Wilson interval, not the verdict.
 *
 * Build:
 *   gcc -O2 -pthread -o dfs_integrity dfs_integrity.c \
 *       -I$DAOS/include -L$DAOS/lib64 -ldfs -ldaos -ldaos_common -lgurt -lm \
 *       -Wl,-rpath,$DAOS/lib64
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <daos.h>
#include <daos_fs.h>
#include <daos_fs_sys.h>

#define PAGE 4096
#define HDRLEN 36               /* the connector's 8 B prefix + 28 B metadata */

/* ---- parameters ---------------------------------------------------------- */
static size_t   g_chunk   = 28ul << 20;
static int      g_threads = 16;
static int      g_rounds  = 13;
static size_t   g_off     = HDRLEN;
static bool     g_burst   = true;
static int      g_sflags  = 0;
static bool     g_perthr  = false;
static long     g_delay_ms = 0;
static bool     g_fresh   = false;
static bool     g_qwrite  = false;
static bool     g_noread  = false;
static bool     g_nowrite = false;
static bool     g_verify_only = false;
static int      g_vpasses = 1;
static const char *g_pool = "gdspool";
static const char *g_cont = "kvlmc";

/* ---- shared state -------------------------------------------------------- */
static dfs_sys_t        *g_sys;
static pthread_barrier_t g_barrier;
static pthread_mutex_t   g_log = PTHREAD_MUTEX_INITIALIZER;
static long              g_reads, g_bad, g_short;
static long              g_retry_ok, g_retry_bad;

/* ---- tagged payload ------------------------------------------------------ */
static inline uint64_t tag_of(int tid, int round, size_t off)
{
	return ((uint64_t)(tid & 0xff) << 56) |
	       ((uint64_t)(round & 0xff) << 48) |
	       (off & 0x0000ffffffffffffULL);
}

static inline void tag_decode(uint64_t w, int *tid, int *round, size_t *off)
{
	*tid   = (int)(w >> 56);
	*round = (int)((w >> 48) & 0xff);
	*off   = (size_t)(w & 0x0000ffffffffffffULL);
}

static void fill_tagged(void *buf, int tid, int round, size_t n)
{
	uint64_t *w = buf;
	for (size_t i = 0; i < n / 8; i++)
		w[i] = tag_of(tid, round, i * 8);
}

/*
 * Diagnose a wrong region by decoding the tags in it: the first defective word
 * plus a census of the whole buffer, since a bad read is usually a mix of
 * correct data and one wrong stretch.
 */
static void diagnose(const void *got, size_t nbytes, int tid, int round,
		     char *out, size_t outsz)
{
	const uint64_t *w = got;
	size_t nwords = nbytes / 8;
	size_t n_ok = 0, n_zero = 0, n_tid = 0, n_round = 0, n_off = 0, n_junk = 0;
	size_t first_bad = (size_t)-1;
	int f_tid = -1, f_round = -1;
	size_t f_off = 0;

	for (size_t i = 0; i < nwords; i++) {
		uint64_t v = w[i];
		size_t want_off = i * 8;
		int t, r;
		size_t o;

		if (v == tag_of(tid, round, want_off)) { n_ok++; continue; }
		if (first_bad == (size_t)-1) {
			first_bad = want_off;
			tag_decode(v, &f_tid, &f_round, &f_off);
		}
		if (v == 0) { n_zero++; continue; }

		tag_decode(v, &t, &r, &o);
		if (t >= g_threads)             n_junk++;
		else if (t != tid)              n_tid++;
		else if (r != round)            n_round++;
		else if (o != want_off)         n_off++;
		else                            n_junk++;
	}

	char detail[160] = "clean";
	if (first_bad != (size_t)-1) {
		if (f_tid == 0 && f_round == 0 && f_off == 0)
			snprintf(detail, sizeof(detail), "zeros");
		else if (f_tid == tid && f_round == round)
			snprintf(detail, sizeof(detail),
				 "own data from offset %zu (%+lld)",
				 f_off, (long long)f_off - (long long)first_bad);
		else if (f_tid == tid)
			snprintf(detail, sizeof(detail),
				 "own data from round %d (this is round %d), offset %zu",
				 f_round, round, f_off);
		else if (f_tid < g_threads)
			snprintf(detail, sizeof(detail),
				 "object t%d's data (round %d, offset %zu)",
				 f_tid, f_round, f_off);
		else
			snprintf(detail, sizeof(detail), "untagged bytes");
	}

	snprintf(out, outsz,
		 "first bad word at %zu%s: %s | words ok=%zu zero=%zu "
		 "foreign-obj=%zu stale-round=%zu shifted=%zu junk=%zu",
		 first_bad == (size_t)-1 ? 0 : first_bad,
		 (first_bad != (size_t)-1 &&
		  ((first_bad + g_off) % (4ul << 20)) == 0) ? " (4 MiB aligned)" : "",
		 detail, n_ok, n_zero, n_tid, n_round, n_off, n_junk);
}

/*
 * Audit an object without knowing what wrote it: every word must carry this
 * object's tid, its own offset, and the same round as word 0.
 */
static bool audit_tagged(const void *got, size_t nbytes, int tid,
			 char *out, size_t outsz)
{
	const uint64_t *w = got;
	int t0, r0;
	size_t o0;

	tag_decode(w[0], &t0, &r0, &o0);
	if (t0 != tid || o0 != 0) {
		diagnose(got, nbytes, tid, r0, out, outsz);
		return false;
	}
	for (size_t i = 0; i < nbytes / 8; i++)
		if (w[i] != tag_of(tid, r0, i * 8)) {
			diagnose(got, nbytes, tid, r0, out, outsz);
			return false;
		}
	snprintf(out, outsz, "round %d, intact", r0);
	return true;
}

/* Wilson score interval, so a rate is never reported as a bare fraction. */
static void wilson(long k, long n, double *lo, double *hi)
{
	const double z = 1.959964;
	if (n == 0) { *lo = *hi = 0.0; return; }
	double p = (double)k / (double)n;
	double z2n = z * z / (double)n;
	double centre = p + z2n / 2.0;
	double margin = z * sqrt(p * (1.0 - p) / (double)n + z2n / (4.0 * (double)n));
	*lo = (centre - margin) / (1.0 + z2n);
	*hi = (centre + margin) / (1.0 + z2n);
	if (*lo < 0.0) *lo = 0.0;
	if (*hi > 1.0) *hi = 1.0;
}

static void obj_path(char *buf, size_t n, int tid, int round)
{
	if (g_fresh)
		snprintf(buf, n, "/cint_o%zu_t%d_r%d", g_off, tid, round);
	else
		snprintf(buf, n, "/cint_o%zu_t%d", g_off, tid);
}

struct targ {
	int        tid;
	dfs_sys_t *sys;
	int        rc;
};

static void *worker(void *p)
{
	struct targ *a = p;
	int tid = a->tid;
	dfs_sys_t *sys = a->sys;
	unsigned char *src = NULL, *dst = NULL;
	unsigned char header[HDRLEN];
	char path[64], detail[256];
	bool dead = false;
	int rc;

	/*
	 * A thread that hits an error keeps taking part in the barriers -- it
	 * just stops doing I/O. Leaving early would hang every other thread
	 * and turn one error into a dead run.
	 */
	if (posix_memalign((void **)&src, PAGE, g_chunk) ||
	    posix_memalign((void **)&dst, PAGE, g_chunk)) {
		a->rc = ENOMEM;
		dead = true;
	}
	memset(header, 'm', sizeof(header));

	for (int r = 0; r < g_rounds; r++) {
		dfs_obj_t *obj = NULL;
		daos_size_t sz;

		obj_path(path, sizeof(path), tid, r);
		if (!dead)
			fill_tagged(src, tid, r, g_chunk);

		if (!dead && !g_nowrite) {
			rc = dfs_sys_open(sys, path, S_IFREG | 0644,
					  O_RDWR | O_CREAT, 0, 0, NULL, &obj);
			if (rc) { a->rc = rc; dead = true; }
			if (!dead && g_off) {
				sz = sizeof(header);
				rc = dfs_sys_write(sys, obj, header, 0, &sz, NULL);
				if (rc) { a->rc = rc; dead = true; }
			}
			if (!dead) {
				sz = g_chunk;
				rc = dfs_sys_write(sys, obj, src, g_off, &sz, NULL);
				if (rc) { a->rc = rc; dead = true; }
			}
			if (obj) { dfs_sys_close(obj); obj = NULL; }
		}

		/*
		 * burst: hold every thread until all writes are done, then
		 * release the reads together. loop mode -- each thread
		 * interleaving its own write and read -- hides the failure far
		 * more often, which is why an early 80/80 PASS was believed.
		 */
		if (g_burst)
			pthread_barrier_wait(&g_barrier);
		if (dead || g_noread)
			continue;

		/*
		 * -d separates two different faults: if a pause makes the
		 * corruption go away the reads are racing something that
		 * settles, and if it does not the read hands back bytes that
		 * were never stored.
		 */
		if (g_delay_ms) {
			struct timespec ts = {
				.tv_sec  = g_delay_ms / 1000,
				.tv_nsec = (g_delay_ms % 1000) * 1000000L,
			};
			nanosleep(&ts, NULL);
		}

		/* Poison, so a read that returns nothing cannot pass. */
		memset(dst, 0xA5, g_chunk);

		rc = dfs_sys_open(sys, path, 0, O_RDONLY, 0, 0, NULL, &obj);
		if (rc) { a->rc = rc; dead = true; continue; }
		sz = g_chunk;
		rc = dfs_sys_read(sys, obj, dst, g_off, &sz, NULL);
		dfs_sys_close(obj);
		if (rc) { a->rc = rc; dead = true; continue; }

		pthread_mutex_lock(&g_log);
		g_reads++;
		pthread_mutex_unlock(&g_log);

		if (sz != g_chunk) {
			pthread_mutex_lock(&g_log);
			g_short++; g_bad++;
			printf("  SHORT   t%-2d r%-2d %zu != %zu\n",
			       tid, r, (size_t)sz, g_chunk);
			pthread_mutex_unlock(&g_log);
			continue;
		}
		/*
		 * In read-only mode (-M) this thread did not write, so the
		 * round tag on disk is whatever wrote it -- audit the tags for
		 * self-consistency instead of comparing against this round.
		 */
		if (g_nowrite) {
			if (audit_tagged(dst, g_chunk, tid, detail, sizeof(detail)))
				continue;
		} else {
			if (memcmp(dst, src, g_chunk) == 0)
				continue;
			diagnose(dst, g_chunk, tid, r, detail, sizeof(detail));
		}

		/*
		 * Read the same object again straight away. The other threads
		 * are still hammering, so this is not a quiet re-read: it asks
		 * only whether the wrong answer sticks.
		 */
		const char *retry = "not tried";
		dfs_obj_t *robj = NULL;
		if (dfs_sys_open(sys, path, 0, O_RDONLY, 0, 0, NULL, &robj) == 0) {
			daos_size_t rsz = g_chunk;

			char rdetail[256];

			memset(dst, 0x5A, g_chunk);
			rc = dfs_sys_read(sys, robj, dst, g_off, &rsz, NULL);
			dfs_sys_close(robj);
			bool ok = rc == 0 && rsz == g_chunk &&
				  (g_nowrite
					? audit_tagged(dst, g_chunk, tid,
						       rdetail, sizeof(rdetail))
					: memcmp(dst, src, g_chunk) == 0);
			retry = ok ? "clean" : "STILL WRONG";
		}

		pthread_mutex_lock(&g_log);
		g_bad++;
		if (!strcmp(retry, "clean"))            g_retry_ok++;
		else if (!strcmp(retry, "STILL WRONG")) g_retry_bad++;
		printf("  CORRUPT t%-2d r%-2d %s | retry=%s\n",
		       tid, r, detail, retry);
		fflush(stdout);
		pthread_mutex_unlock(&g_log);
	}

	free(src);
	free(dst);
	return NULL;
}

/*
 * Write every object single-threaded with nothing else running: the control
 * for the whole investigation. If objects written with no concurrency at all
 * read back wrong, no client-side threading explains the bug.
 */
static int quiet_write_all(dfs_sys_t *sys)
{
	unsigned char *src = NULL, header[HDRLEN];
	int bad = 0;

	if (posix_memalign((void **)&src, PAGE, g_chunk)) {
		fprintf(stderr, "quiet write: out of memory\n");
		return -1;
	}
	memset(header, 'm', sizeof(header));

	for (int tid = 0; tid < g_threads; tid++) {
		dfs_obj_t *obj = NULL;
		daos_size_t sz;
		char path[64];
		int rc;

		obj_path(path, sizeof(path), tid, 0);
		fill_tagged(src, tid, 0, g_chunk);

		rc = dfs_sys_open(sys, path, S_IFREG | 0644, O_RDWR | O_CREAT,
				  0, 0, NULL, &obj);
		if (rc) {
			printf("  qwrite t%d: open rc=%d\n", tid, rc);
			bad++;
			continue;
		}
		if (g_off) {
			sz = sizeof(header);
			rc = dfs_sys_write(sys, obj, header, 0, &sz, NULL);
			if (rc) { printf("  qwrite t%d: hdr rc=%d\n", tid, rc); bad++; }
		}
		sz = g_chunk;
		rc = dfs_sys_write(sys, obj, src, g_off, &sz, NULL);
		if (rc || sz != g_chunk) {
			printf("  qwrite t%d: rc=%d wrote=%zu\n", tid, rc, (size_t)sz);
			bad++;
		}
		dfs_sys_close(obj);
	}
	free(src);
	return bad;
}

/*
 * Read every object once, single-threaded, nothing else running, and audit its
 * tags. The discriminator: wrong here means the stored state or the quiet read
 * path is at fault; right here means the concurrent reads returned data that
 * was never stored.
 */
static int verify_at_rest(dfs_sys_t *sys, int pass)
{
	unsigned char *got = NULL;
	int bad = 0;

	if (posix_memalign((void **)&got, PAGE, g_chunk)) {
		fprintf(stderr, "at-rest: out of memory\n");
		return -1;
	}

	for (int tid = 0; tid < g_threads; tid++) {
		dfs_obj_t *obj = NULL;
		daos_size_t sz = g_chunk;
		char path[64], detail[256];
		int rc;

		obj_path(path, sizeof(path), tid, g_rounds - 1);
		memset(got, 0xA5, g_chunk);

		rc = dfs_sys_open(sys, path, 0, O_RDONLY, 0, 0, NULL, &obj);
		if (rc) {
			printf("  at-rest p%d t%d: open rc=%d\n", pass, tid, rc);
			bad++;
			continue;
		}
		rc = dfs_sys_read(sys, obj, got, g_off, &sz, NULL);
		dfs_sys_close(obj);
		if (rc || sz != g_chunk) {
			printf("  at-rest p%d t%d: read rc=%d size=%zu\n",
			       pass, tid, rc, (size_t)sz);
			bad++;
			continue;
		}
		if (!audit_tagged(got, g_chunk, tid, detail, sizeof(detail))) {
			printf("  at-rest p%d t%d: WRONG -- %s\n", pass, tid, detail);
			bad++;
		}
	}
	free(got);
	return bad;
}

static void usage(const char *me)
{
	fprintf(stderr,
		"usage: %s [-p pool] [-c cont] [-s MiB] [-t threads] [-r rounds] "
		"[-o payload_off] [-m burst|loop] [-f sflags] [-H 0|1] [-d ms] "
		"[-F 0|1] [-W] [-N] [-M] [-Q] [-V passes]\n", me);
}

int main(int argc, char **argv)
{
	int opt, rc, ret = 0;
	struct timespec t0, t1;

	while ((opt = getopt(argc, argv, "p:c:s:t:r:o:m:f:H:d:F:V:WNMQh")) != -1) {
		switch (opt) {
		case 'p': g_pool = optarg; break;
		case 'c': g_cont = optarg; break;
		case 's': g_chunk = (size_t)strtoul(optarg, NULL, 0) << 20; break;
		case 't': g_threads = atoi(optarg); break;
		case 'r': g_rounds = atoi(optarg); break;
		case 'o': g_off = (size_t)strtoul(optarg, NULL, 0); break;
		case 'm': g_burst = strcmp(optarg, "burst") == 0; break;
		case 'f': g_sflags = atoi(optarg); break;
		case 'H': g_perthr = atoi(optarg) != 0; break;
		case 'd': g_delay_ms = strtol(optarg, NULL, 0); break;
		case 'F': g_fresh = atoi(optarg) != 0; break;
		case 'V': g_vpasses = atoi(optarg); break;
		case 'W': g_qwrite = true; break;
		case 'N': g_noread = true; break;
		case 'M': g_nowrite = true; break;
		case 'Q': g_verify_only = true; break;
		default: usage(argv[0]); return 2;
		}
	}
	/* tid and round each occupy one byte of every tag. */
	if (g_threads < 1 || g_threads > 255 || g_rounds < 1 || g_rounds > 255 ||
	    g_chunk < PAGE) {
		usage(argv[0]);
		return 2;
	}
	if (g_off && g_off < HDRLEN) {
		fprintf(stderr, "payload offset %zu overlaps the %d B header; "
			"use 0 to skip it\n", g_off, HDRLEN);
		return 2;
	}

	rc = daos_init();
	if (rc) { fprintf(stderr, "daos_init: %d\n", rc); return 2; }
	rc = dfs_init();                /* else dfs_sys_connect returns EACCES */
	if (rc) { fprintf(stderr, "dfs_init: %d\n", rc); return 2; }

	printf("pool=%s cont=%s size=%zuMiB threads=%d rounds=%d payload_off=%zu "
	       "(%s) mode=%s sflags=%d handles=%s delay=%ldms paths=%s\n",
	       g_pool, g_cont, g_chunk >> 20, g_threads, g_rounds, g_off,
	       (g_off % (4ul << 20)) == 0 ? "chunk-aligned" : "straddling",
	       g_burst ? "burst" : "loop", g_sflags,
	       g_perthr ? "per-thread" : "shared", g_delay_ms,
	       g_fresh ? "fresh per round" : "reused");
	printf("phases: quiet-write=%s concurrent-write=%s concurrent-read=%s "
	       "verify-passes=%d\n",
	       g_qwrite ? "yes" : "no",
	       (g_verify_only || g_nowrite) ? "no" : "yes",
	       (g_verify_only || g_noread) ? "no" : "yes", g_vpasses);

	rc = dfs_sys_connect(g_pool, NULL, g_cont, O_RDWR, g_sflags, NULL, &g_sys);
	if (rc) { fprintf(stderr, "dfs_sys_connect: %d\n", rc); return 2; }

	pthread_barrier_init(&g_barrier, NULL, g_threads);

	pthread_t *th = calloc(g_threads, sizeof(*th));
	struct targ *args = calloc(g_threads, sizeof(*args));
	for (int i = 0; i < g_threads; i++) {
		args[i].tid = i;
		args[i].sys = g_sys;
		if (g_perthr) {
			/*
			 * A private handle per thread takes dfs_sys's
			 * open-handle cache out of the picture entirely (the
			 * bug already survives DFS_SYS_NO_CACHE, §3).
			 */
			rc = dfs_sys_connect(g_pool, NULL, g_cont, O_RDWR,
					     g_sflags, NULL, &args[i].sys);
			if (rc) {
				fprintf(stderr, "dfs_sys_connect(t%d): %d\n", i, rc);
				return 2;
			}
		}
	}

	if (g_qwrite) {
		int qbad = quiet_write_all(g_sys);
		printf("quiet single-threaded write: %d error(s)\n", qbad);
	}

	clock_gettime(CLOCK_MONOTONIC, &t0);
	if (!g_verify_only) {
		for (int i = 0; i < g_threads; i++)
			pthread_create(&th[i], NULL, worker, &args[i]);
		for (int i = 0; i < g_threads; i++)
			pthread_join(th[i], NULL);
	}
	clock_gettime(CLOCK_MONOTONIC, &t1);

	for (int i = 0; i < g_threads; i++)
		if (args[i].rc) {
			fprintf(stderr, "thread %d failed: rc=%d\n", i, args[i].rc);
			ret = 2;
		}

	int atrest = 0;
	for (int v = 0; v < g_vpasses; v++) {
		int bad = verify_at_rest(g_sys, v);

		printf("at-rest pass %d: %s (%d/%d objects wrong when read alone)\n",
		       v, bad == 0 ? "clean" : "CORRUPT", bad, g_threads);
		if (bad > 0)
			atrest += bad;
	}

	double secs = (t1.tv_sec - t0.tv_sec) + (t1.tv_nsec - t0.tv_nsec) / 1e9;
	double lo, hi;

	wilson(g_bad, g_reads, &lo, &hi);
	printf("%s: %ld/%ld concurrent reads corrupt (%.2f%%, 95%% CI %.2f-%.2f%%)"
	       ", %ld short, retry clean/wrong %ld/%ld, at-rest bad %d, %.1f s, "
	       "%.2f GB/s\n",
	       (g_bad || atrest) ? "FAIL" : "PASS", g_bad, g_reads,
	       g_reads ? 100.0 * g_bad / g_reads : 0.0, 100.0 * lo, 100.0 * hi,
	       g_short, g_retry_ok, g_retry_bad, atrest, secs,
	       g_reads ? (double)g_reads * g_chunk / secs / 1e9 : 0.0);

	if ((g_bad || atrest) && ret == 0)
		ret = 1;

	for (int i = 0; g_perthr && i < g_threads; i++)
		dfs_sys_disconnect(args[i].sys);
	dfs_sys_disconnect(g_sys);
	dfs_fini();
	daos_fini();
	return ret;
}
