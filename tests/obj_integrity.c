/*
 * obj_integrity.c -- the dfs_integrity.c workload rebuilt on the raw object
 * API, with no DFS and no dc_array (plan §6.2).
 *
 * Purpose: dfs_integrity.c reproduces silent read corruption through
 * dfs_sys_read -> dc_array, where one 28 MiB read fans out into seven child
 * OBJ_FETCH tasks. If the same corruption appears here -- where each 4 MiB
 * chunk is its own daos_obj_fetch() with one dkey, one IOD, one recx, and no
 * array layer at all -- then dc_array.c and the DFS stack are exonerated and
 * the fault sits at or below the object layer. If it does NOT appear here,
 * the child-task fan-out inside one array read becomes the prime suspect.
 *
 * Layout per thread: one object, dkey = chunk index (uint64), akey "A",
 * DAOS_IOD_ARRAY with a single recx covering the whole 4 MiB chunk.
 * Payload words are the same self-describing tags as dfs_integrity.c:
 * (tid << 56) | (round << 48) | absolute_payload_offset, so a wrong region
 * still names its own origin.
 *
 *   -s MiB   object size (default 28 -> 7 chunks of 4 MiB)
 *   -k MiB   chunk size  (default 4)
 *   -t N     threads     (default 16)
 *   -r N     rounds      (default 40)
 *   -T N     tid base (multi-process arm)
 *   -o CLASS object class (default RP_2G4; use RP_2G1 on 2-target pools)
 *   -m MODE  burst | loop
 *   -p POOL -c CONT      (container may be plain or POSIX; object class is
 *                         resolved with daos_oclass_name2id)
 *
 * Build:
 *   gcc -O2 -pthread -o obj_integrity obj_integrity.c \
 *       -I$DAOS/include -L$DAOS/lib64 -ldaos -ldaos_common -lgurt -lm \
 *       -Wl,-rpath,$DAOS/lib64
 */
#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <fcntl.h>
#include <math.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <daos.h>
#include <daos_obj_class.h>

/*
 * Object layout (rank/target per shard).  The prototype lives in the
 * internal header daos/object.h, which the install tree does not ship,
 * so it is declared here.  ABI matches libdaos 2.8/2.9.
 */
struct daos_shard_loc { uint32_t sd_rank; uint32_t sd_tgt_idx; };
struct daos_obj_shard { uint32_t os_replica_nr; struct daos_shard_loc os_shard_loc[0]; };
struct daos_obj_layout { uint32_t ol_ver; uint32_t ol_class; uint32_t ol_nr;
			 struct daos_obj_shard *ol_shards[0]; };
int daos_obj_layout_get(daos_handle_t coh, daos_obj_id_t oid, struct daos_obj_layout **layout);
int daos_obj_layout_free(struct daos_obj_layout *layout);

#define PAGE 4096

static size_t g_objsz  = 28ul << 20;
static size_t g_chunk  = 4ul << 20;
static int    g_threads = 16;
static int    g_rounds  = 40;
static int    g_tid_base = 0;
/*
 * -A <round>: audit only.  Skip the workload entirely and re-read every
 * object serially, single threaded, expecting the tag of <round>.  Run as a
 * separate process after a corrupting run, this answers whether a chunk that
 * came back as another object's data is still wrong once all concurrency is
 * gone -- i.e. whether the logical-to-physical mapping is persistently wrong,
 * or the corruption only exists inside the concurrent window.
 */
static int    g_audit = -1;
static bool   g_burst  = true;
static const char *g_pool = "gdspool";
static const char *g_cont = "ci_obj";
static const char *g_oclass = "RP_2G4";

static daos_handle_t     g_poh, g_coh;
static pthread_barrier_t g_barrier;
static pthread_mutex_t   g_log = PTHREAD_MUTEX_INITIALIZER;
static long              g_reads, g_bad;

static inline uint64_t tag_of(int tid, int round, size_t off)
{
	return ((uint64_t)(tid & 0xff) << 56) |
	       ((uint64_t)(round & 0xff) << 48) |
	       (off & 0x0000ffffffffffffULL);
}

static void fill_tagged(void *buf, int tid, int round, size_t base, size_t n)
{
	uint64_t *w = buf;
	for (size_t i = 0; i < n / 8; i++)
		w[i] = tag_of(tid, round, base + i * 8);
}

/* Same census as dfs_integrity.c, over one whole object buffer. */
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
		if (t != tid && o == want)          n_tid++;
		else if (t == tid && r != round)    n_round++;
		else if (t == tid && o != want)     n_off++;
		else if (t != tid && (o % 8) == 0 && o < (1ULL << 36)) n_tid++;
		else                                n_junk++;
	}
	snprintf(out, outsz,
		 "first bad word at %zu%s: t%d r%d off %zu | "
		 "ok=%zu zero=%zu foreign-obj=%zu stale-round=%zu shifted=%zu junk=%zu",
		 first_bad == (size_t)-1 ? 0 : first_bad,
		 (first_bad != (size_t)-1 && (first_bad % g_chunk) == 0)
			? " (chunk-aligned)" : "",
		 f_tid, f_round, f_off,
		 n_ok, n_zero, n_tid, n_round, n_off, n_junk);
}

struct targ {
	int          tid;
	daos_obj_id_t oid;
	int          rc;
};

/* One chunk = one update/fetch: dkey = chunk index, akey "A", one recx. */
static int chunk_io(daos_handle_t oh, bool write, uint64_t chunk_idx,
		    void *buf, size_t len)
{
	daos_key_t dkey;
	daos_iod_t iod = {0};
	daos_recx_t recx;
	d_sg_list_t sgl;
	d_iov_t sg_iov;
	uint64_t dk = chunk_idx;

	d_iov_set(&dkey, &dk, sizeof(dk));
	d_iov_set(&iod.iod_name, "A", 1);
	iod.iod_type = DAOS_IOD_ARRAY;
	iod.iod_size = 1;
	iod.iod_nr   = 1;
	recx.rx_idx  = 0;
	recx.rx_nr   = len;
	iod.iod_recxs = &recx;

	d_iov_set(&sg_iov, buf, len);
	sgl.sg_nr = 1;
	sgl.sg_nr_out = 0;
	sgl.sg_iovs = &sg_iov;

	if (write)
		return daos_obj_update(oh, DAOS_TX_NONE, 0, &dkey, 1, &iod,
				       &sgl, NULL);
	return daos_obj_fetch(oh, DAOS_TX_NONE, 0, &dkey, 1, &iod, &sgl,
			      NULL, NULL);
}

static void *worker(void *p)
{
	struct targ *a = p;
	int tid = a->tid;
	unsigned char *src = NULL, *dst = NULL;
	char detail[256];
	daos_handle_t oh;
	size_t nchunks = g_objsz / g_chunk;
	bool dead = false;
	int rc;

	if (posix_memalign((void **)&src, PAGE, g_objsz) ||
	    posix_memalign((void **)&dst, PAGE, g_objsz)) {
		a->rc = ENOMEM;
		dead = true;
	}

	rc = daos_obj_open(g_coh, a->oid, DAOS_OO_RW, &oh, NULL);
	if (rc) { a->rc = rc; dead = true; }

	for (int r = 0; r < g_rounds; r++) {
		if (!dead) {
			fill_tagged(src, tid, r, 0, g_objsz);
			for (size_t c = 0; c < nchunks && !dead; c++) {
				rc = chunk_io(oh, true, c, src + c * g_chunk,
					      g_chunk);
				if (rc) { a->rc = rc; dead = true; }
			}
		}

		if (g_burst)
			pthread_barrier_wait(&g_barrier);
		if (dead)
			continue;

		memset(dst, 0xA5, g_objsz);
		for (size_t c = 0; c < nchunks && !dead; c++) {
			rc = chunk_io(oh, false, c, dst + c * g_chunk, g_chunk);
			if (rc) { a->rc = rc; dead = true; }
		}
		if (dead)
			continue;

		pthread_mutex_lock(&g_log);
		g_reads++;
		pthread_mutex_unlock(&g_log);

		if (memcmp(dst, src, g_objsz) == 0)
			continue;

		diagnose(dst, g_objsz, tid, r, detail, sizeof(detail));

		/* Does the wrong answer stick, under continuing load? */
		const char *retry = "not tried";
		memset(dst, 0x5A, g_objsz);
		bool ok = true;
		for (size_t c = 0; c < nchunks && ok; c++)
			if (chunk_io(oh, false, c, dst + c * g_chunk, g_chunk))
				ok = false;
		if (ok)
			retry = memcmp(dst, src, g_objsz) == 0
				? "clean" : "STILL WRONG";

		pthread_mutex_lock(&g_log);
		g_bad++;
		printf("  CORRUPT t%-2d r%-2d %s | retry=%s\n",
		       tid, r, detail, retry);
		fflush(stdout);
		pthread_mutex_unlock(&g_log);
	}

	daos_obj_close(oh, NULL);
	free(src);
	free(dst);
	return NULL;
}

int main(int argc, char **argv)
{
	int opt, rc, ret = 0;

	while ((opt = getopt(argc, argv, "p:c:s:k:t:r:T:o:m:A:h")) != -1) {
		switch (opt) {
		case 'p': g_pool = optarg; break;
		case 'c': g_cont = optarg; break;
		case 's': g_objsz = (size_t)strtoul(optarg, NULL, 0) << 20; break;
		case 'k': g_chunk = (size_t)strtoul(optarg, NULL, 0) << 20; break;
		case 't': g_threads = atoi(optarg); break;
		case 'r': g_rounds = atoi(optarg); break;
		case 'T': g_tid_base = atoi(optarg); break;
		case 'o': g_oclass = optarg; break;
		case 'm': g_burst = strcmp(optarg, "burst") == 0; break;
		case 'A': g_audit = atoi(optarg); break;
		default:
			fprintf(stderr, "usage: %s [-p pool] [-c cont] [-s MiB]"
				" [-k chunkMiB] [-t n] [-r n] [-T base]"
				" [-o oclass]"
				" [-m burst|loop] [-A round]\n", argv[0]);
			return 2;
		}
	}
	if (g_objsz % g_chunk || g_threads < 1 || g_threads > 255 ||
	    g_rounds < 1 || g_rounds > 255 ||
	    g_tid_base < 0 || g_tid_base + g_threads > 255) {
		fprintf(stderr, "bad geometry\n");
		return 2;
	}

	rc = daos_init();
	if (rc) { fprintf(stderr, "daos_init: %d\n", rc); return 2; }
	rc = daos_pool_connect(g_pool, NULL, DAOS_PC_RW, &g_poh, NULL, NULL);
	if (rc) { fprintf(stderr, "pool_connect: %d\n", rc); return 2; }
	rc = daos_cont_open(g_poh, g_cont, DAOS_COO_RW, &g_coh, NULL, NULL);
	if (rc) { fprintf(stderr, "cont_open: %d\n", rc); return 2; }

	daos_oclass_id_t cid = daos_oclass_name2id(g_oclass);
	if (cid == OC_UNKNOWN) { fprintf(stderr, "bad oclass\n"); return 2; }

	printf("pool=%s cont=%s obj=%zuMiB chunk=%zuMiB threads=%d rounds=%d "
	       "tid_base=%d oclass=%s mode=%s  (raw daos_obj_fetch, no DFS)\n",
	       g_pool, g_cont, g_objsz >> 20, g_chunk >> 20, g_threads,
	       g_rounds, g_tid_base, g_oclass, g_burst ? "burst" : "loop");

	pthread_barrier_init(&g_barrier, NULL, g_threads);
	pthread_t *th = calloc(g_threads, sizeof(*th));
	struct targ *args = calloc(g_threads, sizeof(*args));

	for (int i = 0; i < g_threads; i++) {
		args[i].tid = g_tid_base + i;
		/*
		 * Deterministic lo bits so reruns hit the same objects (the
		 * overwrite-generation behaviour we want), distinct per tid.
		 */
		args[i].oid.lo = 0xC0FFEE00u + (unsigned)args[i].tid;
		args[i].oid.hi = 0;
		rc = daos_obj_generate_oid(g_coh, &args[i].oid, 0, cid, 0, 0);
		if (rc) { fprintf(stderr, "generate_oid: %d\n", rc); return 2; }
	}

	/* Where does each object live?  Needed to attribute foreign data. */
	for (int i = 0; i < g_threads; i++) {
		struct daos_obj_layout *lay = NULL;

		rc = daos_obj_layout_get(g_coh, args[i].oid, &lay);
		printf("LAYOUT t%-2d oid=%" PRIx64 ".%" PRIx64, args[i].tid,
		       args[i].oid.hi, args[i].oid.lo);
		if (rc || lay == NULL) { printf(" layout_get rc=%d\n", rc); continue; }
		printf(" ver=%u class=%u grps=%u", lay->ol_ver, lay->ol_class, lay->ol_nr);
		for (uint32_t g = 0; g < lay->ol_nr; g++) {
			struct daos_obj_shard *sh = lay->ol_shards[g];
			for (uint32_t r = 0; sh && r < sh->os_replica_nr; r++)
				printf(" rank=%u tgt=%u", sh->os_shard_loc[r].sd_rank,
				       sh->os_shard_loc[r].sd_tgt_idx);
		}
		printf("\n");
		daos_obj_layout_free(lay);
	}
	fflush(stdout);

	if (g_audit >= 0) {
		unsigned char *dst = NULL;
		char detail[256];
		size_t nchunks = g_objsz / g_chunk;

		if (posix_memalign((void **)&dst, PAGE, g_objsz)) return 2;
		printf("AUDIT: quiescent serial re-read, expecting round %d\n",
		       g_audit);
		for (int i = 0; i < g_threads; i++) {
			daos_handle_t oh;
			bool ok = true;

			rc = daos_obj_open(g_coh, args[i].oid, DAOS_OO_RO, &oh,
					   NULL);
			if (rc) {
				printf("  AUDIT t%-2d obj_open rc=%d\n",
				       args[i].tid, rc);
				ret = 2;
				continue;
			}
			memset(dst, 0xA5, g_objsz);
			for (size_t c = 0; c < nchunks && ok; c++)
				if (chunk_io(oh, false, c, dst + c * g_chunk,
					     g_chunk))
					ok = false;
			daos_obj_close(oh, NULL);
			if (!ok) {
				printf("  AUDIT t%-2d fetch failed\n",
				       args[i].tid);
				ret = 2;
				continue;
			}
			g_reads++;
			{
				bool clean = true;
				uint64_t *w = (uint64_t *)dst;

				for (size_t k = 0; k < g_objsz / 8; k++)
					if (w[k] != tag_of(args[i].tid, g_audit,
							   k * 8)) {
						clean = false;
						break;
					}
				if (clean)
					continue;
			}
			diagnose(dst, g_objsz, args[i].tid, g_audit, detail,
				 sizeof(detail));
			g_bad++;
			printf("  AUDIT t%-2d STILL WRONG | %s\n",
			       args[i].tid, detail);
		}
		free(dst);
		printf("%s: %ld/%ld objects still wrong when quiescent\n",
		       g_bad ? "AUDIT-FAIL" : "AUDIT-PASS", g_bad, g_reads);
		goto done;
	}

	for (int i = 0; i < g_threads; i++)
		pthread_create(&th[i], NULL, worker, &args[i]);
	for (int i = 0; i < g_threads; i++)
		pthread_join(th[i], NULL);

	for (int i = 0; i < g_threads; i++)
		if (args[i].rc) {
			fprintf(stderr, "thread %d failed: rc=%d\n",
				i, args[i].rc);
			ret = 2;
		}

	printf("%s: %ld/%ld reads corrupt (raw object API)\n",
	       g_bad ? "FAIL" : "PASS", g_bad, g_reads);

done:
	if (g_bad && ret == 0) ret = 1;

	daos_cont_close(g_coh, NULL);
	daos_pool_disconnect(g_poh, NULL);
	daos_fini();
	return ret;
}
