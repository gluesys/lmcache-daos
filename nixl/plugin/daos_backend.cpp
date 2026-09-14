/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Gluesys Co., Ltd.
 */
#include "daos_backend.h"

#include <cstdlib>
#include <sstream>
#include <vector>

#include "common/nixl_log.h"

namespace {

/*
 * metaInfo grammar, kept deliberately small:
 *
 *   "pool/container"                 open (or reuse) the container, then
 *                                    generate an object id from devId
 *   "pool/container/<hi>.<lo>"       use that exact object id
 *
 * The two-field form is what a cache uses: the caller already has a unique
 * devId per object, so deriving the oid from it keeps the caller from having
 * to invent and remember object ids. The three-field form exists so an
 * existing object can be addressed, which is what any test that writes with
 * one tool and reads with another needs.
 */
struct daosTarget {
    std::string pool;
    std::string cont;
    bool haveOid = false;
    daos_obj_id_t oid{};
};

bool
parseTarget(const std::string &meta, daosTarget &out) {
    std::vector<std::string> parts;
    std::stringstream ss(meta);
    std::string tok;

    while (std::getline(ss, tok, '/'))
        if (!tok.empty()) parts.push_back(tok);

    if (parts.size() < 2 || parts.size() > 3) return false;

    out.pool = parts[0];
    out.cont = parts[1];

    if (parts.size() == 3) {
        const auto dot = parts[2].find('.');
        if (dot == std::string::npos) return false;
        try {
            out.oid.hi = std::stoull(parts[2].substr(0, dot));
            out.oid.lo = std::stoull(parts[2].substr(dot + 1));
        }
        catch (const std::exception &) {
            return false;
        }
        out.haveOid = true;
    }
    return true;
}

} // namespace

nixlDaosThreadPool::nixlDaosThreadPool(unsigned n) {
    workers_.reserve(n);
    for (unsigned i = 0; i < n; i++) {
        workers_.emplace_back([this] {
            for (;;) {
                std::function<void()> job;
                {
                    std::unique_lock<std::mutex> g(m_);
                    cv_.wait(g, [this] { return stop_ || !q_.empty(); });
                    if (stop_ && q_.empty()) return;
                    job = std::move(q_.front());
                    q_.pop();
                }
                job();
            }
        });
    }
}

nixlDaosThreadPool::~nixlDaosThreadPool() {
    {
        std::lock_guard<std::mutex> g(m_);
        stop_ = true;
    }
    cv_.notify_all();
    for (auto &t : workers_)
        if (t.joinable()) t.join();
}

void
nixlDaosThreadPool::submit(std::function<void()> job) {
    {
        std::lock_guard<std::mutex> g(m_);
        q_.push(std::move(job));
    }
    cv_.notify_one();
}

nixl_b_params_t
nixlDaosEngine::getPluginParams() {
    /* Nothing yet. "pool" and "container" will land here so a deployment can
     * set a default instead of repeating it in every metaInfo. */
    return nixl_b_params_t();
}

nixlDaosEngine::nixlDaosEngine(const nixlBackendInitParams *init_params)
    : nixlBackendEngine(init_params) {
    const int rc = daos_init();

    /* -DER_ALREADY: another component in this process already called it. That
     * is normal when the application also links the DAOS client, so it counts
     * as success for us -- but we must then not call daos_fini() on the way
     * out, or we pull the library out from under them. */
    if (rc == 0) {
        daosInited_ = true;
    } else if (rc != -DER_ALREADY) {
        NIXL_ERROR << "daos_init() failed: " << rc;
    }

    /* 64 is where the concurrency ladder flattens on the testbed: at 400G
     * verbs the unfolded arm reaches 8.3 GB/s at 16 threads, 11.3 at 32,
     * 14.4 at 64, and 14.3 at 128. Effective concurrency is min(threads,
     * requests in flight), so a caller that keeps fewer requests outstanding
     * is capped by its own depth rather than by this number. */
    unsigned nthreads = 64;
    if (const char *env = std::getenv("NIXL_DAOS_THREADS")) {
        const int v = std::atoi(env);
        if (v > 0 && v <= 512) nthreads = static_cast<unsigned>(v);
        else NIXL_WARN << "NIXL_DAOS_THREADS=" << env << " ignored (expected 1..512)";
    }
    pool_ = std::make_unique<nixlDaosThreadPool>(nthreads);
    NIXL_DEBUG << "DAOS backend: " << nthreads << " IO threads";
}

nixlDaosEngine::~nixlDaosEngine() {
    /* Workers touch container handles through the object handles they were
     * given, so they have to be gone before anything below is closed. */
    pool_.reset();

    {
        std::lock_guard<std::mutex> g(mtx_);
        for (auto &kv : conts_) {
            if (kv.second.refs > 0)
                NIXL_WARN << "DAOS container " << kv.first << " still had "
                          << kv.second.refs << " registration(s) at shutdown";
            daos_cont_close(kv.second.coh, nullptr);
            daos_pool_disconnect(kv.second.poh, nullptr);
        }
        conts_.clear();
    }
    if (daosInited_) daos_fini();
}

nixl_status_t
nixlDaosEngine::getCont(const std::string &pool,
                        const std::string &cont,
                        contHandles *&out) {
    const std::string key = pool + "/" + cont;
    auto it = conts_.find(key);

    if (it != conts_.end()) {
        it->second.refs++;
        out = &it->second;
        return NIXL_SUCCESS;
    }

    contHandles h{};
    int rc = daos_pool_connect(pool.c_str(), nullptr, DAOS_PC_RW, &h.poh, nullptr, nullptr);
    if (rc != 0) {
        NIXL_ERROR << "daos_pool_connect(" << pool << ") failed: " << rc;
        return NIXL_ERR_BACKEND;
    }

    rc = daos_cont_open(h.poh, cont.c_str(), DAOS_COO_RW, &h.coh, nullptr, nullptr);
    if (rc != 0) {
        NIXL_ERROR << "daos_cont_open(" << cont << ") failed: " << rc;
        daos_pool_disconnect(h.poh, nullptr);
        return NIXL_ERR_BACKEND;
    }

    h.refs = 1;
    out = &conts_.emplace(key, h).first->second;
    return NIXL_SUCCESS;
}

void
nixlDaosEngine::putCont(const std::string &pool, const std::string &cont) {
    const std::string key = pool + "/" + cont;
    auto it = conts_.find(key);
    if (it == conts_.end()) return;

    if (--it->second.refs > 0) return;

    daos_cont_close(it->second.coh, nullptr);
    daos_pool_disconnect(it->second.poh, nullptr);
    conts_.erase(it);
}

nixl_status_t
nixlDaosEngine::registerMem(const nixlBlobDesc &mem,
                            const nixl_mem_t &nixl_mem,
                            nixlBackendMD *&out) {
    out = nullptr;

    /* Host and device buffers are the transfer source/sink, not something DAOS
     * holds: the buffer is handed to the fetch as an sgl at transfer time and
     * needs no registration of its own. Accept them so the agent can pair one
     * with a FILE descriptor, and return no metadata.
     *
     * Device memory needs no registration here either. CaRT registers the
     * buffer when it builds the bulk handle, unless a pre-registered RDMA key
     * is supplied in daos_mem_attr_t::ma_rkey -- which is the path that avoids
     * double registration when cuFile already owns the buffer, and is left for
     * when there is a cuFile-registered pool to test against. */
    if (nixl_mem == DRAM_SEG) return NIXL_SUCCESS;
#ifdef NIXL_DAOS_HAVE_GPU
    if (nixl_mem == VRAM_SEG) return NIXL_SUCCESS;
#endif

    if (nixl_mem != FILE_SEG) return NIXL_ERR_NOT_SUPPORTED;

    daosTarget t;
    if (!parseTarget(mem.metaInfo, t)) {
        NIXL_ERROR << "DAOS metaInfo must be \"pool/container\" or "
                      "\"pool/container/<hi>.<lo>\", got: "
                   << mem.metaInfo;
        return NIXL_ERR_INVALID_PARAM;
    }

    std::lock_guard<std::mutex> g(mtx_);

    contHandles *ch = nullptr;
    const nixl_status_t st = getCont(t.pool, t.cont, ch);
    if (st != NIXL_SUCCESS) return st;

    daos_obj_id_t oid = t.oid;
    if (!t.haveOid) {
        /* Derive the object id from devId. Deterministic on purpose: the same
         * devId must name the same object across process restarts, otherwise a
         * cache cannot find what it stored. */
        oid.hi = 0;
        oid.lo = mem.devId;
        const int rc =
            daos_obj_generate_oid(ch->coh, &oid, DAOS_OT_MULTI_HASHED, OC_UNKNOWN, 0, 0);
        if (rc != 0) {
            NIXL_ERROR << "daos_obj_generate_oid failed: " << rc;
            putCont(t.pool, t.cont);
            return NIXL_ERR_BACKEND;
        }
    }

    daos_handle_t oh;
    const int rc = daos_obj_open(ch->coh, oid, DAOS_OO_RW, &oh, nullptr);
    if (rc != 0) {
        NIXL_ERROR << "daos_obj_open failed: " << rc;
        putCont(t.pool, t.cont);
        return NIXL_ERR_BACKEND;
    }

    out = new nixlDaosObjMD(t.pool, t.cont, oh, oid, mem.devId);
    return NIXL_SUCCESS;
}

nixl_status_t
nixlDaosEngine::deregisterMem(nixlBackendMD *meta) {
    /* DRAM registrations produced no metadata; nothing to undo. */
    if (meta == nullptr) return NIXL_SUCCESS;

    auto *md = dynamic_cast<nixlDaosObjMD *>(meta);
    if (md == nullptr) return NIXL_ERR_INVALID_PARAM;

    std::lock_guard<std::mutex> g(mtx_);

    const int rc = daos_obj_close(md->oh_, nullptr);
    if (rc != 0) NIXL_ERROR << "daos_obj_close failed: " << rc;

    putCont(md->pool_, md->cont_);
    delete md;

    return rc == 0 ? NIXL_SUCCESS : NIXL_ERR_BACKEND;
}

/* ---- transfer ------------------------------------------------------------ */

/*
 * Group the paired descriptors by (object, dkey) and build the DAOS structures
 * once. Nothing is submitted here.
 *
 * Pairing follows the convention every storage backend in NIXL uses and POSIX
 * documents by example: local and remote are walked together by index, local
 * is the DRAM buffer and remote is the storage side, so remote->addr is the
 * offset and remote->len the length.
 */
nixl_status_t
nixlDaosEngine::prepXfer(const nixl_xfer_op_t &operation,
                         const nixl_meta_dlist_t &local,
                         const nixl_meta_dlist_t &remote,
                         const std::string &remote_agent,
                         nixlBackendReqH *&handle,
                         const nixl_opt_b_args_t *opt_args) const {
    handle = nullptr;

    if (local.descCount() != remote.descCount()) {
        NIXL_ERROR << "DAOS: descriptor counts differ (local " << local.descCount()
                   << ", remote " << remote.descCount() << ")";
        return NIXL_ERR_INVALID_PARAM;
    }
    if (local.descCount() == 0) return NIXL_ERR_INVALID_PARAM;

    /* First pass: bucket by (object handle, dkey) without touching the DAOS
     * structures, so the vectors can be sized exactly before any pointer into
     * them is taken. */
    struct entry {
        uint64_t akey;
        size_t len;
        void *buf;
        uint64_t localDev; /* CUDA ordinal when the local side is VRAM */
    };
    std::map<std::pair<uint64_t, uint64_t>, std::pair<daos_handle_t, std::vector<entry>>>
        buckets;

    auto li = local.begin();
    auto ri = remote.begin();
    for (; li != local.end() && ri != remote.end(); ++li, ++ri) {
        auto *md = dynamic_cast<nixlDaosObjMD *>(ri->metadataP);
        if (md == nullptr) {
            NIXL_ERROR << "DAOS: remote descriptor has no DAOS metadata; was it "
                          "registered with FILE_SEG?";
            return NIXL_ERR_INVALID_PARAM;
        }
        if (li->len != ri->len) {
            NIXL_ERROR << "DAOS: paired descriptors differ in length (" << li->len
                       << " vs " << ri->len << ")";
            return NIXL_ERR_INVALID_PARAM;
        }

        const uint64_t dkey = ri->addr / dkeySpan_;
        const uint64_t akey = ri->addr % dkeySpan_;
        auto &slot = buckets[{md->devId_, dkey}];
        slot.first = md->oh_;
        slot.second.push_back(
            {akey, ri->len, reinterpret_cast<void *>(li->addr), li->devId});
    }

    auto req = std::make_unique<nixlDaosBackendReqH>();
    req->op = operation;
    req->groups.reserve(buckets.size());

#ifdef NIXL_DAOS_HAVE_GPU
    /* The descriptor list carries the segment type, so the local side tells us
     * whether these buffers are device memory. devId on a VRAM descriptor is
     * the CUDA ordinal. */
    const bool localIsGpu = local.getType() == VRAM_SEG;
#endif

    for (auto &kv : buckets) {
        const auto &ents = kv.second.second;
        nixlDaosIoGroup g;
        g.oh = kv.second.first;
        g.dkeyVal = kv.first.second;

        const size_t n = ents.size();
        g.akeyVals.resize(n);
        g.iods.resize(n);
        g.recxs.resize(n);
        g.sgls.resize(n);
        g.iovs.resize(n);

        for (size_t i = 0; i < n; i++) {
            g.akeyVals[i] = ents[i].akey;

            g.recxs[i].rx_idx = 0;
            g.recxs[i].rx_nr = ents[i].len;

            g.iods[i] = daos_iod_t{};
            d_iov_set(&g.iods[i].iod_name, &g.akeyVals[i], sizeof(uint64_t));
            g.iods[i].iod_type = DAOS_IOD_ARRAY;
            g.iods[i].iod_size = 1; /* byte records */
            g.iods[i].iod_nr = 1;
            g.iods[i].iod_recxs = &g.recxs[i];

            d_iov_set(&g.iovs[i], ents[i].buf, ents[i].len);
            g.sgls[i].sg_nr = 1;
            g.sgls[i].sg_nr_out = 0;
            g.sgls[i].sg_iovs = &g.iovs[i];
        }

#ifdef NIXL_DAOS_HAVE_GPU
        if (localIsGpu) {
            g.memAttrs.resize(n);
            for (size_t i = 0; i < n; i++) {
                g.memAttrs[i] = daos_mem_attr_t{};
                g.memAttrs[i].ma_mem_type = DAOS_MEM_TYPE_CUDA;
                g.memAttrs[i].ma_device_id = ents[i].localDev;
                /* ma_rkey left zeroed: CaRT registers the buffer itself. */
            }
        }
#endif
        req->groups.push_back(std::move(g));
    }

    NIXL_DEBUG << "DAOS: prepared " << local.descCount() << " descriptor(s) as "
               << req->groups.size() << " RPC(s)";

    handle = req.release();
    return NIXL_SUCCESS;
}

/*
 * Submit. Each group is one daos_obj_fetch()/daos_obj_update().
 *
 * The work runs on a thread rather than a DAOS event queue. That is a
 * deliberate first cut: the event path serialises on the per-EQ eqx_lock and
 * has been measured to cap around 7-12 GB/s however the queues are arranged,
 * whereas blocking calls on a pool of threads reach the full 26 GB/s. One
 * thread per request is still coarse -- a pool belongs here once there is a
 * throughput test to size it against -- but prepXfer() has already folded the
 * descriptor list down to a handful of RPCs, so the thread count tracks
 * requests rather than descriptors.
 */
nixl_status_t
nixlDaosEngine::postXfer(const nixl_xfer_op_t &operation,
                         const nixl_meta_dlist_t &local,
                         const nixl_meta_dlist_t &remote,
                         const std::string &remote_agent,
                         nixlBackendReqH *&handle,
                         const nixl_opt_b_args_t *opt_args) const {
    auto *req = dynamic_cast<nixlDaosBackendReqH *>(handle);
    if (req == nullptr) return NIXL_ERR_INVALID_PARAM;
    if (req->groups.empty()) return NIXL_SUCCESS;

    req->status.store(NIXL_SUCCESS);
    req->pending.store(static_cast<unsigned>(req->groups.size()));
    req->posted = true;

    for (auto &g : req->groups) {
        nixlDaosIoGroup *gp = &g;
        pool_->submit([req, gp]() {
            daos_key_t dkey;
            d_iov_set(&dkey, &gp->dkeyVal, sizeof(uint64_t));

            const unsigned nr = static_cast<unsigned>(gp->iods.size());
            int rc;
#ifdef NIXL_DAOS_HAVE_GPU
            if (!gp->memAttrs.empty()) {
                /* This once said "synchronous only -- no event queue", and
                 * used that to justify putting the work on a thread. It was
                 * wrong. In theodore/b_cufile the GPU entry points take an
                 * event and hand it to the same task machinery as the host
                 * ones -- src/client/api/object.c:213 differs from :197 only
                 * by the GPU_DIRECT flag and args->mem_attrs:
                 *
                 *   update      dc_obj_update_task_create(oh, th, flags, ..., ev, ...)
                 *   update_gpu  dc_obj_update_task_create(oh, th, flags|GPU_DIRECT, ..., ev, ...)
                 *
                 * So the nullptr below is a choice, not a constraint, and it
                 * is currently the wrong one: doc/FAILURE-MODES.md measured the
                 * event queue as the only bounded way out of a dead engine
                 * (16/16 threads lost blocking, 0/16 with a poll timeout). The
                 * VRAM_SEG path can have that escape; it just does not yet. */
                rc = req->op == NIXL_READ
                         ? daos_obj_fetch_gpu(gp->oh, DAOS_TX_NONE, 0, &dkey, nr,
                                              gp->iods.data(), gp->sgls.data(),
                                              gp->memAttrs.data(), nullptr, nullptr)
                         : daos_obj_update_gpu(gp->oh, DAOS_TX_NONE, 0, &dkey, nr,
                                               gp->iods.data(), gp->sgls.data(),
                                               gp->memAttrs.data(), nullptr);
            } else
#endif
            {
                rc = req->op == NIXL_READ
                         ? daos_obj_fetch(gp->oh, DAOS_TX_NONE, 0, &dkey, nr,
                                          gp->iods.data(), gp->sgls.data(), nullptr,
                                          nullptr)
                         : daos_obj_update(gp->oh, DAOS_TX_NONE, 0, &dkey, nr,
                                           gp->iods.data(), gp->sgls.data(), nullptr);
            }

            if (rc != 0) {
                NIXL_ERROR << (req->op == NIXL_READ ? "daos_obj_fetch" : "daos_obj_update")
                           << " failed: " << rc;
                req->recordError(NIXL_ERR_BACKEND);
            } else if (req->op == NIXL_READ) {
                /* A fetch of a key that was never written returns success with
                 * iod_size 0. Silence there would be a miss reported as a hit,
                 * and this project has already paid for one of those. */
                for (size_t i = 0; i < gp->iods.size(); i++) {
                    if (gp->iods[i].iod_size == 0) {
                        NIXL_ERROR << "DAOS: read of an absent key (dkey " << gp->dkeyVal
                                   << ", akey " << gp->akeyVals[i] << ")";
                        req->recordError(NIXL_ERR_NOT_FOUND);
                        break;
                    }
                }
            }
            req->finishOne();
        });
    }

    return NIXL_IN_PROG;
}

nixl_status_t
nixlDaosEngine::checkXfer(nixlBackendReqH *handle) const {
    auto *req = dynamic_cast<nixlDaosBackendReqH *>(handle);
    if (req == nullptr) return NIXL_ERR_INVALID_PARAM;

    /* Prepared but not posted: nothing is running, so nothing can have
     * finished. Saying SUCCESS here would let a caller act on data that was
     * never fetched. */
    if (!req->posted) return NIXL_IN_PROG;
    if (req->pending.load() != 0) return NIXL_IN_PROG;

    return static_cast<nixl_status_t>(req->status.load());
}

nixl_status_t
nixlDaosEngine::releaseReqH(nixlBackendReqH *handle) const {
    auto *req = dynamic_cast<nixlDaosBackendReqH *>(handle);
    if (req == nullptr) return NIXL_ERR_INVALID_PARAM;

    /* DAOS has no cancel for a blocking call already in flight, so the only
     * safe way out is to let it finish: the group vectors the workers read
     * from die with this object. */
    if (req->posted) req->waitDone();

    delete req;
    return NIXL_SUCCESS;
}
