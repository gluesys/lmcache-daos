/*
 * posix_integrity.c -- does the corruption reach ordinary POSIX file I/O?
 *
 * obj_integrity.c drives daos_obj_fetch and dfs_integrity.c drives the DFS API.
 * Neither is how people actually write data: they mount with dfuse and use
 * open/pwrite/pread. This runs the same tagged workload through a plain POSIX
 * path so the answer applies to real workloads.
 *
 * Every 8-byte word is (tid<<56)|(round<<48)|offset, so a wrong word names the
 * thread and round it came from, and a whole-chunk substitution is obvious.
 *
 * dfuse and the kernel page cache can both serve a read from memory and hide a
 * bad block, so mount with --disable-caching, and use -A for a verify-only pass
 * from a separate process after the writers are gone.
 *
 *   ./posix_integrity -d /mnt/dfuse [-s MiB] [-k chunkMiB] [-t n] [-r n] [-A round]
 *
 *   -A round   verify only: reopen every file and check it against <round>,
 *              single threaded, no writes -- the POSIX equivalent of
 *              obj_integrity's quiescent audit
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <unistd.h>
#include <fcntl.h>
#include <pthread.h>
#include <errno.h>

#define PAGE 4096

static const char *g_dir   = "/mnt/dfuse";
static size_t      g_objsz = 28ul << 20;
static size_t      g_chunk = 4ul << 20;
static int         g_threads = 16;
static int         g_rounds  = 10;
static int         g_audit   = -1;
static int         g_direct;
/*
 * -L n repeats the whole round set n times.  Needed because the round is only
 * 8 bits of the tag, so rounds cannot exceed 255, yet a 4 KiB file has to write
 * far more iterations than a 28 MiB one to move a comparable number of bytes --
 * which is itself the answer to "why does a text file look fine".
 */
static int         g_loops   = 1;

static pthread_barrier_t g_barrier;
static pthread_mutex_t   g_log = PTHREAD_MUTEX_INITIALIZER;
static long              g_reads, g_bad;

static inline uint64_t tag_of(int tid, int round, size_t off)
{
	return ((uint64_t)(tid & 0xff) << 56) |
	       ((uint64_t)(round & 0xff) << 48) |
	       (off & 0x0000ffffffffffffULL);
}

static void fill_tagged(void *buf, int tid, int round, size_t n)
{
	uint64_t *w = buf;
	for (size_t i = 0; i < n / 8; i++)
		w[i] = tag_of(tid, round, i * 8);
}

/* same census as the other reproducers, so results are comparable */
static void diagnose(const void *got, size_t nbytes, int tid, int round,
		     char *out, size_t outsz)
{
	const uint64_t *w = got;
	size_t n_ok = 0, n_zero = 0, n_tid = 0, n_round = 0, n_off = 0, n_junk = 0;
	size_t first_bad = (size_t)-1;
	int f_tid = -1, f_round = -1;
	size_t f_off = 0;

	for (size_t i = 0; i < nbytes / 8; i++) {
		uint64_t v = w[i];
		size_t want = i * 8;
		int t, r;
		size_t o;

		if (v == tag_of(tid, round, want)) { n_ok++; continue; }
		if (first_bad == (size_t)-1) {
			first_bad = want;
			f_tid = (int)(v >> 56);
			f_round = (int)((v >> 48) & 0xff);
			f_off = v & 0x0000ffffffffffffULL;
		}
		if (v == 0) { n_zero++; continue; }
		t = (int)(v >> 56); r = (int)((v >> 48) & 0xff);
		o = v & 0x0000ffffffffffffULL;
		if (t != tid && o == want)       n_tid++;
		else if (t == tid && r != round) n_round++;
		else if (t == tid && o != want)  n_off++;
		else                             n_junk++;
	}
	snprintf(out, outsz,
		 "first bad word at %zu%s: t%d r%d off %zu | ok=%zu zero=%zu "
		 "foreign-file=%zu stale-round=%zu shifted=%zu junk=%zu",
		 first_bad == (size_t)-1 ? 0 : first_bad,
		 (first_bad != (size_t)-1 && (first_bad % g_chunk) == 0)
			? " (chunk-aligned)" : "",
		 f_tid, f_round, f_off,
		 n_ok, n_zero, n_tid, n_round, n_off, n_junk);
}

static int open_file(int tid, bool rw)
{
	char path[512];
	int flags = rw ? (O_RDWR | O_CREAT) : O_RDONLY;

	snprintf(path, sizeof(path), "%s/pi_%03d.dat", g_dir, tid);
	if (g_direct)
		flags |= O_DIRECT;
	return open(path, flags, 0644);
}

static int write_all(int fd, const void *buf, size_t len, off_t off)
{
	const char *p = buf;
	while (len) {
		ssize_t n = pwrite(fd, p, len, off);
		if (n <= 0) return -1;
		p += n; off += n; len -= (size_t)n;
	}
	return 0;
}

static int read_all(int fd, void *buf, size_t len, off_t off)
{
	char *p = buf;
	while (len) {
		ssize_t n = pread(fd, p, len, off);
		if (n <= 0) return -1;
		p += n; off += n; len -= (size_t)n;
	}
	return 0;
}

struct targ { int tid; int rc; };

static void *worker(void *p)
{
	struct targ *a = p;
	unsigned char *src = NULL, *dst = NULL;
	char detail[256];
	size_t nchunks = g_objsz / g_chunk;
	int fd;

	if (posix_memalign((void **)&src, PAGE, g_objsz) ||
	    posix_memalign((void **)&dst, PAGE, g_objsz)) {
		a->rc = ENOMEM;
		return NULL;
	}
	fd = open_file(a->tid, true);
	if (fd < 0) { a->rc = errno; return NULL; }

	for (int l = 0; l < g_loops; l++)
	for (int r = 0; r < g_rounds; r++) {
		fill_tagged(src, a->tid, r, g_objsz);
		for (size_t c = 0; c < nchunks; c++)
			if (write_all(fd, src + c * g_chunk, g_chunk,
				      (off_t)(c * g_chunk))) {
				a->rc = errno; goto out;
			}
		/* all writers land in the same window, like the other reproducers */
		pthread_barrier_wait(&g_barrier);

		memset(dst, 0xA5, g_objsz);
		for (size_t c = 0; c < nchunks; c++)
			if (read_all(fd, dst + c * g_chunk, g_chunk,
				     (off_t)(c * g_chunk))) {
				a->rc = errno; goto out;
			}

		pthread_mutex_lock(&g_log);
		g_reads++;
		pthread_mutex_unlock(&g_log);

		if (memcmp(dst, src, g_objsz) == 0)
			continue;

		diagnose(dst, g_objsz, a->tid, r, detail, sizeof(detail));
		pthread_mutex_lock(&g_log);
		g_bad++;
		printf("  CORRUPT t%-2d r%-2d %s\n", a->tid, r, detail);
		fflush(stdout);
		pthread_mutex_unlock(&g_log);
	}
out:
	close(fd);
	free(src); free(dst);
	return NULL;
}

static int audit(void)
{
	unsigned char *dst = NULL;
	char detail[256];
	size_t nchunks = g_objsz / g_chunk;
	int ret = 0;

	if (posix_memalign((void **)&dst, PAGE, g_objsz))
		return 2;
	printf("AUDIT: quiescent serial re-read via POSIX, expecting round %d\n",
	       g_audit);
	for (int tid = 0; tid < g_threads; tid++) {
		int fd = open_file(tid, false);
		bool clean = true;
		uint64_t *w;

		if (fd < 0) {
			printf("  AUDIT t%-2d open failed: %s\n", tid, strerror(errno));
			ret = 2;
			continue;
		}
		memset(dst, 0xA5, g_objsz);
		for (size_t c = 0; c < nchunks; c++)
			if (read_all(fd, dst + c * g_chunk, g_chunk,
				     (off_t)(c * g_chunk))) {
				printf("  AUDIT t%-2d read failed\n", tid);
				ret = 2;
				clean = false;
				break;
			}
		close(fd);
		if (!clean)
			continue;
		g_reads++;
		w = (uint64_t *)dst;
		for (size_t k = 0; k < g_objsz / 8; k++)
			if (w[k] != tag_of(tid, g_audit, k * 8)) {
				clean = false;
				break;
			}
		if (clean)
			continue;
		diagnose(dst, g_objsz, tid, g_audit, detail, sizeof(detail));
		g_bad++;
		printf("  AUDIT t%-2d STILL WRONG | %s\n", tid, detail);
	}
	free(dst);
	printf("%s: %ld/%ld files still wrong when quiescent\n",
	       g_bad ? "AUDIT-FAIL" : "AUDIT-PASS", g_bad, g_reads);
	return g_bad ? 1 : ret;
}

int main(int argc, char **argv)
{
	int opt, ret = 0;

	while ((opt = getopt(argc, argv, "d:s:k:S:K:t:r:L:A:Oh")) != -1) {
		switch (opt) {
		case 'd': g_dir = optarg; break;
		case 's': g_objsz = (size_t)strtoul(optarg, NULL, 0) << 20; break;
		case 'k': g_chunk = (size_t)strtoul(optarg, NULL, 0) << 20; break;
		case 'S': g_objsz = (size_t)strtoul(optarg, NULL, 0) << 10; break;
		case 'K': g_chunk = (size_t)strtoul(optarg, NULL, 0) << 10; break;
		case 'L': g_loops = atoi(optarg); break;
		case 't': g_threads = atoi(optarg); break;
		case 'r': g_rounds = atoi(optarg); break;
		case 'A': g_audit = atoi(optarg); break;
		case 'O': g_direct = 1; break;
		default:
			fprintf(stderr, "usage: %s -d <dir> [-s MiB] [-k chunkMiB]"
				" [-S KiB] [-K KiB] [-t n] [-r n] [-L loops]"
				" [-A round] [-O]\n", argv[0]);
			return 2;
		}
	}
	if (g_objsz % g_chunk || g_threads < 1 || g_threads > 255 ||
	    g_rounds < 1 || g_rounds > 255) {
		fprintf(stderr, "bad geometry\n");
		return 2;
	}
	printf("dir=%s file=%zuKiB chunk=%zuKiB threads=%d rounds=%d loops=%d%s"
	       "  written=%.1fMiB/thread  (plain POSIX pwrite/pread)\n",
	       g_dir, g_objsz >> 10, g_chunk >> 10, g_threads, g_rounds, g_loops,
	       g_direct ? " O_DIRECT" : "",
	       (double)g_objsz * g_rounds * g_loops / (1024 * 1024));

	if (g_audit >= 0)
		return audit();

	pthread_barrier_init(&g_barrier, NULL, g_threads);
	pthread_t *th = calloc(g_threads, sizeof(*th));
	struct targ *args = calloc(g_threads, sizeof(*args));

	for (int i = 0; i < g_threads; i++) {
		args[i].tid = i;
		pthread_create(&th[i], NULL, worker, &args[i]);
	}
	for (int i = 0; i < g_threads; i++)
		pthread_join(th[i], NULL);
	for (int i = 0; i < g_threads; i++)
		if (args[i].rc) {
			fprintf(stderr, "thread %d failed: %s\n", i,
				strerror(args[i].rc));
			ret = 2;
		}
	printf("%s: %ld/%ld reads corrupt (POSIX via dfuse)\n",
	       g_bad ? "FAIL" : "PASS", g_bad, g_reads);
	if (g_bad && ret == 0) ret = 1;
	return ret;
}
